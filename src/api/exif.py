import csv
import fcntl
import os
import datetime
import time

import duckdb


LIBRARY_ROOT = '/photo'
EXIF_CSV = '/storage/exif.csv'
# append-only logs of "<photo_id>,<value>,<time>"; the latest row for a photo wins
REVIEWS_CSV = '/storage/reviews.csv'
FAVORITES_CSV = '/storage/favorites.csv'
ROTATIONS_CSV = '/storage/rotations.csv'
# the same, with a path in place of the photo id
DELETIONS_CSV = '/storage/deletions.csv'
APPROVED = 'approved'
REJECTED = 'rejected'
UNREVIEWED = 'unreviewed'
REVIEWED = 'reviewed'
FAVORITE = 'favorite'
NOT_FAVORITE = 'not_favorite'
DELETED = 'deleted'
RESTORED = 'restored'
# photos this close together from one camera are reviewed as one, up to a limit so that a
# long run of different shots isn't all decided by one of them
BURST_GAP_SECS = 5
BURST_MAX_PHOTOS = 12
# cameras with unset clocks give dates like 1901:01:01, which shouldn't appear as years of photos
LIBRARY_EARLIEST_YEAR = 1902
# a photo's "YYYY:MM" in the library, or NULL if its date can't be trusted
LIBRARY_MONTH = f"""
    CASE WHEN regexp_matches(DateTimeOriginal, '^\\d{{4}}:(0[1-9]|1[0-2]):')
        AND LEFT(DateTimeOriginal, 4) >= '{LIBRARY_EARLIEST_YEAR}'
    THEN LEFT(DateTimeOriginal, 7) END
"""
db = duckdb.connect(database=':memory:', read_only=False)
_log_sizes = {}
# deleted paths that were left out when the index was last loaded
_left_out_of_index = set()


def load_csv(force_reload=False):
    # Load the CSV data into a DuckDB table, and keep only one row for each photo.
    # Copies / alternate formats of a photo share a base name and capture time;
    # base names alone are reused by cameras and phones.  Reviews are keyed on this
    # identity rather than a path, since which copy is kept can change between loads
    if force_reload or update_available() or _left_out_path_returned():
        _load_deletions(force_reload=True)
        _left_out_of_index.clear()
        _left_out_of_index.update(_deleted_paths())
        db.execute(
            """
            CREATE OR REPLACE TABLE exif AS
            SELECT
                *,
                ?::TIMESTAMP last_modified,
                LOWER(REGEXP_REPLACE(FileName, '\\.[^.]*$', '')) || '|' || DateTimeOriginal AS photo_id
            FROM read_csv_auto(?, sample_size=-1, ignore_errors=true)
            WHERE MIMEType LIKE 'image/%'
            AND DateTimeOriginal IS NOT NULL
            -- so that another copy of a deleted photo can take its place
            AND SourceFile NOT IN (SELECT photo_id FROM deletions)
            -- prefer JPEGs, then PNGs, then HEICs, then everything else
            QUALIFY ROW_NUMBER() OVER (
               PARTITION BY photo_id
               ORDER BY CASE FileType
                    WHEN 'JPEG' THEN 1
                    WHEN 'PNG'  THEN 2
                    WHEN 'HEIC' THEN 3
                    ELSE 4
                END
            ) = 1;
            """,
            [
                last_modified(),
                EXIF_CSV
            ]
        )


def last_modified():
    return datetime.datetime.fromtimestamp(os.path.getmtime(EXIF_CSV))


def update_available():
    if not db.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'exif'").fetchone():
        return True
    # the table is empty if the index holds no images
    loaded = db.execute("SELECT MAX(last_modified) FROM exif").fetchone()[0]
    return loaded is None or last_modified() > loaded


def count():
    return db.execute(f"SELECT COUNT(*) FROM {_with_status()}").fetchone()[0]


def dataframe(query='select * from exif'):
    load_csv()
    return db.execute(query).df()


def tags():
    load_csv()
    return [
        col
        for col in db.execute("PRAGMA table_info('exif');").fetchdf()['name'].tolist()
        if col not in ('last_modified', 'photo_id')
    ]


def _load_log(table, path):
    # logs are shared between workers and only ever grow, so a log's size says whether
    # another worker has written to it since this one last read it
    size = os.path.getsize(path) if os.path.exists(path) else 0
    if size == _log_sizes.get(table):
        return
    if size:
        db.execute(
            f"""
            CREATE OR REPLACE TABLE {table} AS
            SELECT photo_id, value
            FROM read_csv(
                ?,
                header=false,
                delim=',',
                quote='"',
                escape='"',
                columns={{'photo_id': 'VARCHAR', 'value': 'VARCHAR', 'decided_ns': 'BIGINT'}},
                ignore_errors=true
            )
            QUALIFY ROW_NUMBER() OVER (PARTITION BY photo_id ORDER BY decided_ns DESC) = 1;
            """,
            [path]
        )
    else:
        db.execute(f"CREATE OR REPLACE TABLE {table} (photo_id VARCHAR, value VARCHAR)")
    _log_sizes[table] = size


def _load_deletions(force_reload=False):
    """
    Deleted paths are left out of the photos straight away, without the index having to be
    rewritten or reloaded, and whether or not a scan is about to write them back.  A path
    stops counting once its file is back, such as by being moved back by hand
    """
    if force_reload:
        _log_sizes.pop('deletions', None)
    loaded = _log_sizes.get('deletions')
    _load_log('deletions', DELETIONS_CSV)
    if _log_sizes['deletions'] == loaded:
        return
    db.execute("DELETE FROM deletions WHERE photo_id IS NULL OR value IS DISTINCT FROM ?", [DELETED])
    returned = [path for path in _deleted_paths() if os.path.exists(path)]
    db.execute("DELETE FROM deletions WHERE list_contains(?::VARCHAR[], photo_id)", [returned])


def _deleted_paths():
    return {path for path, in db.execute("SELECT photo_id FROM deletions").fetchall()}


def _left_out_path_returned():
    # a restored photo needs its row back, if a load has happened since it was deleted
    if not _left_out_of_index:
        return False
    _load_deletions()
    return bool(_left_out_of_index - _deleted_paths())


def _append_log(path, rows):
    with open(path, 'a', newline='', encoding='utf-8', errors='surrogateescape') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        # taken under the lock, so timestamps follow the order rows were written
        now = time.time_ns()
        writer = csv.writer(fh, lineterminator='\n')
        writer.writerows((photo_id, value, now) for photo_id, value in rows)


def record_deleted(paths):
    # after the files have moved, since a path whose file is still there doesn't count
    _append_log(DELETIONS_CSV, [(path, DELETED) for path in paths])


def record_restored(paths):
    _append_log(DELETIONS_CSV, [(path, RESTORED) for path in paths])


def photos_version():
    """
    Changes whenever the photos to show might have, so that a page can tell without fetching them
    """
    files = (EXIF_CSV, REVIEWS_CSV, FAVORITES_CSV, ROTATIONS_CSV, DELETIONS_CSV)
    stats = [os.stat(path) if os.path.exists(path) else None for path in files]
    return ':'.join(f"{stat.st_mtime_ns}-{stat.st_size}" if stat else '' for stat in stats)


def record_reviews(approved=(), rejected=(), unreviewed=()):
    _append_log(
        REVIEWS_CSV,
        [
            *((photo_id, APPROVED) for photo_id in approved),
            *((photo_id, REJECTED) for photo_id in rejected),
            *((photo_id, UNREVIEWED) for photo_id in unreviewed)
        ]
    )


def reset_reviews(datestring=''):
    """
    Return reviewed photos to unreviewed, and report how many.  Resetting the whole
    library includes photos no longer in the index, so they don't return with an old
    decision if their files do
    """
    _with_status()
    rows = db.execute(
        """
        SELECT photo_id
        FROM reviews LEFT JOIN exif USING (photo_id)
        WHERE value <> ? AND (? OR DateTimeOriginal LIKE ?)
        """,
        [UNREVIEWED, not datestring, f"{datestring}%"]
    ).fetchall()
    _append_log(REVIEWS_CSV, [(photo_id, UNREVIEWED) for photo_id, in rows])
    return len(rows)


def set_favorite(photo_id, favorite):
    _append_log(FAVORITES_CSV, [(photo_id, FAVORITE if favorite else NOT_FAVORITE)])


def set_rotation(photo_id, rotation):
    _append_log(ROTATIONS_CSV, [(photo_id, rotation)])


def _with_status():
    load_csv()
    _load_log('reviews', REVIEWS_CSV)
    _load_log('favorites', FAVORITES_CSV)
    _load_log('rotations', ROTATIONS_CSV)
    _load_deletions()
    return f"""
        (
            SELECT
                exif.*,
                COALESCE(reviews.value, '{UNREVIEWED}') AS review_status,
                COALESCE(favorites.value = '{FAVORITE}', false) AS favorite,
                COALESCE(TRY_CAST(rotations.value AS INTEGER), 0) AS rotation
            FROM exif
            LEFT JOIN reviews USING (photo_id)
            LEFT JOIN favorites USING (photo_id)
            LEFT JOIN rotations USING (photo_id)
            WHERE exif.SourceFile NOT IN (SELECT photo_id FROM deletions)
        )
    """


def background_photo():
    """
    A photo to stand in on a page with nothing to show: a random favorite, or failing
    that any approved photo.  A favorite that has since been hidden doesn't qualify
    """
    row = db.execute(
        f"""
        SELECT SourceFile, rotation FROM {_with_status()}
        WHERE review_status = ?
        ORDER BY favorite DESC, random()
        LIMIT 1
        """,
        [APPROVED]
    ).fetchone()
    return row


def stand_in_photos(exclude, limit, seed):
    """
    Approved photos from any date, for when there aren't enough from around today: favorites
    first, then a selection that stays the same for a given seed
    """
    return db.execute(
        f"""
        SELECT * FROM {_with_status()}
        WHERE review_status = ? AND NOT list_contains(?::VARCHAR[], photo_id)
        ORDER BY favorite DESC, hash(photo_id || ?)
        LIMIT ?
        """,
        [APPROVED, exclude, seed, limit]
    ).df()


def select_photos(datestring='', status=APPROVED):
    return db.execute(
        f"""
        SELECT * FROM {_with_status()}
        WHERE DateTimeOriginal LIKE ? AND review_status = ?
        """,
        [f"{datestring}%", status]
    ).df()


def review_counts(datestring=''):
    rows = db.execute(
        f"""
        SELECT review_status, COUNT(*) FROM {_with_status()}
        WHERE DateTimeOriginal LIKE ?
        GROUP BY review_status
        """,
        [f"{datestring}%"]
    ).fetchall()
    return {APPROVED: 0, REJECTED: 0, UNREVIEWED: 0, **dict(rows)}


def library_months():
    """
    Every month with photos to browse, newest first and undated photos last.  Unlike the gallery
    this includes unreviewed photos, which are most of a library, but not hidden ones.  A month's
    cover stays the same until its photos change: a favorite or approved photo if there is one,
    preferring camera photos over screenshots and JPEGs, which are the quickest to make a
    thumbnail from
    """
    rows = db.execute(
        f"""
        SELECT
            {LIBRARY_MONTH} AS month,
            COUNT(*),
            first(struct_pack(path := SourceFile, rotation := rotation) ORDER BY
                favorite DESC,
                review_status = ? DESC,
                Make IS NOT NULL DESC,
                FileType = 'JPEG' DESC,
                hash(photo_id)
            )
        FROM {_with_status()}
        WHERE review_status <> ?
        GROUP BY month
        ORDER BY month DESC NULLS LAST
        """,
        [APPROVED, REJECTED]
    ).fetchall()
    return [{'month': month, 'photos': photos, 'cover': cover} for month, photos, cover in rows]


def library_photos(month):
    # month as in library_months, where None is the undated photos
    rows = db.execute(
        f"""
        SELECT photo_id, SourceFile, DateTimeOriginal, review_status, favorite, rotation
        FROM {_with_status()}
        WHERE ({LIBRARY_MONTH}) IS NOT DISTINCT FROM ? AND review_status <> ?
        ORDER BY DateTimeOriginal, photo_id
        """,
        [month, REJECTED]
    ).fetchall()
    return [
        {
            'id': photo_id,
            'path': path,
            'taken': taken,
            'status': status,
            'favorite': favorite,
            'rotation': rotation
        }
        for photo_id, path, taken, status, favorite, rotation in rows
    ]


def _review_item(photo_id, path, taken, review_status, rotation, no_camera=False):
    return {
        'id': photo_id,
        'path': path,
        'date': taken.split(' ')[0].replace(':', '/'),
        'status': review_status,
        'rotation': rotation,
        'no_camera': no_camera,
        'burst': []
    }


def review_page(datestring, status, offset, limit):
    if status == UNREVIEWED:
        return _review_queue_page(datestring, offset, limit)
    table = _with_status()
    statuses = [APPROVED, REJECTED] if status == REVIEWED else [status]
    filters = [f"{datestring}%", statuses]
    total = db.execute(
        f"SELECT COUNT(*) FROM {table} WHERE DateTimeOriginal LIKE ? AND list_contains(?, review_status)",
        filters
    ).fetchone()[0]
    # reviewing shrinks the queue, which can leave the offset past its end
    offset = min(offset, max(total - limit, 0))
    rows = db.execute(
        f"""
        SELECT photo_id, SourceFile, DateTimeOriginal, review_status, rotation FROM {table}
        WHERE DateTimeOriginal LIKE ? AND list_contains(?, review_status)
        ORDER BY DateTimeOriginal DESC, photo_id
        LIMIT ? OFFSET ?
        """,
        [*filters, limit, offset]
    ).fetchall()
    return {
        'total': total,
        'photos': total,
        'offset': offset,
        'items': [_review_item(*row) for row in rows]
    }


def _review_queue_page(datestring, offset, limit):
    """
    A page of the queue, which counts bursts of photos as one tile each.  Each tile's item is
    the middle photo of its burst, with the others listed in its 'burst'
    """
    tiles = f"""
        WITH queue AS (
            SELECT
                photo_id, SourceFile, DateTimeOriginal, review_status, rotation, Make, Model,
                TRY_STRPTIME(LEFT(DateTimeOriginal, 19), '%Y:%m:%d %H:%M:%S') AS taken
            FROM {_with_status()}
            WHERE DateTimeOriginal LIKE ? AND review_status = '{UNREVIEWED}'
        ), bursts AS (
            -- with no camera or time to go on, a photo is on its own
            SELECT *, SUM((
                Model IS NULL OR taken IS NULL
                OR COALESCE(date_diff('second', previous_taken, taken) > {BURST_GAP_SECS}, true)
            )::INT) OVER (PARTITION BY Model ORDER BY taken, photo_id ROWS UNBOUNDED PRECEDING) AS burst
            FROM (
                SELECT *, LAG(taken) OVER (PARTITION BY Model ORDER BY taken, photo_id) AS previous_taken
                FROM queue
            )
        ), parts AS (
            SELECT *, (
                ROW_NUMBER() OVER (PARTITION BY Model, burst ORDER BY taken, photo_id) - 1
            ) // {BURST_MAX_PHOTOS} AS part
            FROM bursts
        ), members AS (
            SELECT
                *,
                ROW_NUMBER() OVER tile AS position,
                COUNT(*) OVER (PARTITION BY Model, burst, part) AS size,
                MAX(DateTimeOriginal) OVER (PARTITION BY Model, burst, part) AS latest
            FROM parts
            WINDOW tile AS (PARTITION BY Model, burst, part ORDER BY taken, photo_id)
        ), numbered AS MATERIALIZED (
            SELECT *, DENSE_RANK() OVER (ORDER BY latest DESC, Model, burst, part) AS tile FROM members
        )
    """
    def fetch(start):
        return db.execute(
            f"""
            {tiles}
            SELECT
                (SELECT MAX(tile) FROM numbered), (SELECT COUNT(*) FROM numbered),
                tile, position = (size + 1) // 2 AS shows_tile,
                photo_id, SourceFile, DateTimeOriginal, review_status, rotation,
                Make IS NULL AND Model IS NULL AS no_camera
            FROM numbered
            WHERE tile > ? AND tile <= ?
            ORDER BY tile, position
            """,
            [f"{datestring}%", start, start + limit]
        ).fetchall()

    rows = fetch(offset)
    if rows:
        total, photos = rows[0][:2]
    else:
        total, photos = db.execute(
            f"{tiles} SELECT COALESCE(MAX(tile), 0), COUNT(*) FROM numbered",
            [f"{datestring}%"]
        ).fetchone()
    # reviewing shrinks the queue, which can leave the offset past its end
    fitted = min(offset, max(total - limit, 0))
    if fitted != offset:
        offset = fitted
        rows = fetch(offset)
    items = {}
    others = {}
    for _, _, tile, shows_tile, *fields in rows:
        item = _review_item(*fields)
        if shows_tile:
            items[tile] = item
        else:
            others.setdefault(tile, []).append(item)
    for tile, item in items.items():
        item['burst'] = others.get(tile, [])
    return {'total': total, 'photos': photos, 'offset': offset, 'items': list(items.values())}
