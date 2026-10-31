import csv
import fcntl
import os
import datetime
import glob
import time

import duckdb


LIBRARY_ROOT = '/photo'
EXIF_CSV = '/storage/exif.csv'
# append-only logs of "<photo_id>,<value>,<time>"; the latest row for a photo wins
REVIEWS_CSV = '/storage/reviews.csv'
FAVORITES_CSV = '/storage/favorites.csv'
ROTATIONS_CSV = '/storage/rotations.csv'
APPROVED = 'approved'
REJECTED = 'rejected'
UNREVIEWED = 'unreviewed'
REVIEWED = 'reviewed'
FAVORITE = 'favorite'
NOT_FAVORITE = 'not_favorite'
db = duckdb.connect(database=':memory:', read_only=False)
_log_sizes = {}


def load_csv(force_reload=False):
    # Load the CSV data into a DuckDB table, and keep only one row for each photo.
    # Copies / alternate formats of a photo share a base name and capture time;
    # base names alone are reused by cameras and phones.  Reviews are keyed on this
    # identity rather than a path, since which copy is kept can change between loads
    if force_reload or update_available():
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
    if db.execute("SHOW TABLES").fetchall() == []:
        return True
    # the table is empty if the index holds no images
    loaded = db.execute("SELECT MAX(last_modified) FROM exif").fetchone()[0]
    return loaded is None or last_modified() > loaded


def count():
    load_csv()
    return db.execute("SELECT COUNT(*) FROM exif").fetchone()[0]


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


def filter_csv(keep):
    """
    Rewrite the CSV with only the rows whose SourceFile satisfies keep.  This has to
    work from the CSV itself: the exif table only holds one row per photo, and
    writing that back would permanently drop every other row
    """
    with open(f"{EXIF_CSV}.lock", 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        tmp = f"{EXIF_CSV}.tmp"
        with (
            open(EXIF_CSV, newline='', encoding='utf-8', errors='surrogateescape') as src,
            open(tmp, 'w', newline='', encoding='utf-8', errors='surrogateescape') as dst
        ):
            reader = csv.reader(src)
            writer = csv.writer(dst, lineterminator='\n')
            writer.writerow(next(reader))
            writer.writerows(row for row in reader if row and keep(row[0]))
        os.replace(tmp, EXIF_CSV)


def delete_images(paths):
    filter_csv(lambda source_file: source_file not in paths)


def prune_deleted():
    """
    Remove rows for files that have been deleted or moved
    """
    existing = set(glob.glob(f"{LIBRARY_ROOT}/**/*", recursive=True))
    filter_csv(lambda source_file: source_file in existing)
    load_csv()


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


def _append_log(path, rows):
    with open(path, 'a', newline='', encoding='utf-8') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        # taken under the lock, so timestamps follow the order rows were written
        now = time.time_ns()
        writer = csv.writer(fh, lineterminator='\n')
        writer.writerows((photo_id, value, now) for photo_id, value in rows)


def record_reviews(approved=(), rejected=()):
    _append_log(
        REVIEWS_CSV,
        [
            *((photo_id, APPROVED) for photo_id in approved),
            *((photo_id, REJECTED) for photo_id in rejected)
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


def review_page(datestring, status, offset, limit):
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
        'offset': offset,
        'items': [
            {
                'id': photo_id,
                'path': path,
                'date': taken.split(' ')[0].replace(':', '/'),
                'status': review_status,
                'rotation': rotation
            }
            for photo_id, path, taken, review_status, rotation in rows
        ]
    }
