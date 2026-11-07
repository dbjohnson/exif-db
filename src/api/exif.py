import os
import datetime

import duckdb

from api.copies import HIDDEN as HIDDEN_DUPLICATE
from api.worklog import append_log as _append_log


LIBRARY_ROOT = '/photo'
EXIF_CSV = '/storage/exif.csv'
# append-only logs of "<photo_id>,<value>,<time>"; the latest row for a photo wins
REVIEWS_CSV = '/storage/reviews.csv'
FAVORITES_CSV = '/storage/favorites.csv'
ROTATIONS_CSV = '/storage/rotations.csv'
# dates given to photos by hand, which take the place of what's in the file
DATES_CSV = '/storage/dates.csv'
# where photos were taken, given by hand: "<lat> <lon>", NO_LOCATION, or empty for the photo's own
LOCATIONS_SET_CSV = '/storage/locations_set.csv'
NO_LOCATION = 'none'
# the same, with a path in place of the photo id
DELETIONS_CSV = '/storage/deletions.csv'
MOVIES_HIDDEN_CSV = '/storage/movies_hidden.csv'
# which photos have been dealt with as duplicates, as in api/copies.py
DUPLICATES_CSV = '/storage/duplicates.csv'
# what each photo looks like, by path, written by scripts/find_duplicates.py
FINGERPRINTS_CSV = '/storage/duplicate_fingerprints.csv'
# where the faces are in each photo, written by scripts/find_faces.py
FACES_CSV = '/storage/faces.csv'
NO_FACES = 'none'
APPROVED = 'approved'
REJECTED = 'rejected'
UNREVIEWED = 'unreviewed'
REVIEWED = 'reviewed'
FAVORITE = 'favorite'
NOT_FAVORITE = 'not_favorite'
DELETED = 'deleted'
HIDDEN = 'hidden'
NOT_HIDDEN = 'not_hidden'
RESTORED = 'restored'
# photos this close together from one camera are reviewed as one if they also look alike, up to a
# limit so that a long run of different shots isn't all decided by one of them
BURST_GAP_SECS = 5
BURST_MAX_PHOTOS = 12
# how many of a fingerprint's 64 bits can differ between one photo of a burst and the next.  In this
# library, pairs at 20 show the same scene, and pairs at 22 start to show different ones
BURST_MAX_DIFFERENCE = 20
# what a photo with no date of its own is treated as having
NO_DATE = '0000:00:00 00:00:00'
# cameras with unset clocks give dates like 1901:01:01, which shouldn't appear as years of photos
LIBRARY_EARLIEST_YEAR = 1902
# photos in a range of days, given as the first day and the day after the last, each "YYYY:MM:DD"
RECENT_FILTER = "DateTimeOriginal >= ? AND DateTimeOriginal < ?"
# which camera took a photo, as text with nothing for none, so that every camera has one name to go by
CAMERA_MAKE = "NULLIF(TRIM(CAST(Make AS VARCHAR)), '')"
CAMERA_MODEL = "NULLIF(TRIM(CAST(Model AS VARCHAR)), '')"
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
    # base names alone are reused by cameras and phones.  Without a capture time to go on,
    # copies share a base name and size.  Reviews are keyed on this identity rather than
    # a path, since which copy is kept can change between loads
    if force_reload or update_available() or _left_out_path_returned():
        _load_deletions(force_reload=True)
        _left_out_of_index.clear()
        _left_out_of_index.update(_deleted_paths())
        db.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE scanned AS
            SELECT
                *,
                ?::TIMESTAMP last_modified,
                LOWER(REGEXP_REPLACE(FileName, '\\.[^.]*$', '')) || '|' || CASE
                    WHEN DateTimeOriginal IS NULL OR DateTimeOriginal = '{NO_DATE}' THEN 'size ' || FileSize
                    ELSE DateTimeOriginal
                END AS photo_id
            FROM read_csv_auto(?, sample_size=-1, ignore_errors=true)
            WHERE MIMEType LIKE 'image/%'
            """,
            [
                last_modified(),
                EXIF_CSV
            ]
        )
        # every file of each photo, as only one of them is kept below
        db.execute("CREATE OR REPLACE TABLE photo_files AS SELECT photo_id, SourceFile FROM scanned")
        db.execute(
            f"""
            CREATE OR REPLACE TABLE exif AS
            SELECT * REPLACE (COALESCE(DateTimeOriginal, '{NO_DATE}') AS DateTimeOriginal)
            FROM scanned
            -- so that another copy of a deleted photo can take its place
            WHERE SourceFile NOT IN (SELECT photo_id FROM deletions)
            -- prefer JPEGs, then PNGs, then HEICs, then everything else
            QUALIFY ROW_NUMBER() OVER (
               PARTITION BY photo_id
               ORDER BY CASE FileType
                    WHEN 'JPEG' THEN 1
                    WHEN 'PNG'  THEN 2
                    WHEN 'HEIC' THEN 3
                    ELSE 4
                END,
                -- the same file each time, as different photos can share an identity
                SourceFile
            ) = 1
            """
        )
        db.execute("DROP TABLE scanned")


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


def deleted_paths():
    _load_deletions()
    return _deleted_paths()


def set_movie_hidden(movie_id, hidden):
    _append_log(MOVIES_HIDDEN_CSV, [(movie_id, HIDDEN if hidden else NOT_HIDDEN)])


def hidden_movies():
    _load_log('movies_hidden', MOVIES_HIDDEN_CSV)
    return {
        movie_id
        for movie_id, in db.execute("SELECT photo_id FROM movies_hidden WHERE value = ?", [HIDDEN]).fetchall()
    }


def _left_out_path_returned():
    # a restored photo needs its row back, if a load has happened since it was deleted
    if not _left_out_of_index:
        return False
    _load_deletions()
    return bool(_left_out_of_index - _deleted_paths())


def record_deleted(paths):
    # after the files have moved, since a path whose file is still there doesn't count
    _append_log(DELETIONS_CSV, [(path, DELETED) for path in paths])


def record_restored(paths):
    _append_log(DELETIONS_CSV, [(path, RESTORED) for path in paths])


def photos_version():
    """
    Changes whenever the photos to show might have, so that a page can tell without fetching them
    """
    # fingerprints decide which recent photos count as one burst
    files = (EXIF_CSV, REVIEWS_CSV, FAVORITES_CSV, ROTATIONS_CSV, DATES_CSV, DELETIONS_CSV, FINGERPRINTS_CSV)
    stats = [os.stat(path) if os.path.exists(path) else None for path in files]
    return ':'.join(f"{stat.st_mtime_ns}-{stat.st_size}" if stat else '' for stat in stats)


def _file_stamps(*paths):
    stats = [os.stat(path) if os.path.exists(path) else None for path in paths]
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
    table = _with_status()
    _load_log('duplicates', DUPLICATES_CSV)
    rows = db.execute(
        f"""
        SELECT photo_id
        FROM reviews LEFT JOIN {table} USING (photo_id)
        WHERE value <> ? AND (? OR DateTimeOriginal LIKE ?)
        -- those weren't a decision about the photo, and starting over would bring them all back
        AND NOT (value = ? AND photo_id IN (SELECT photo_id FROM duplicates WHERE value = ?))
        """,
        [UNREVIEWED, not datestring, f"{datestring}%", REJECTED, HIDDEN_DUPLICATE]
    ).fetchall()
    _append_log(REVIEWS_CSV, [(photo_id, UNREVIEWED) for photo_id, in rows])
    return len(rows)


def set_favorites(photo_ids, favorite):
    _append_log(FAVORITES_CSV, [(photo_id, FAVORITE if favorite else NOT_FAVORITE) for photo_id in photo_ids])


def set_rotations(rotations):
    _append_log(ROTATIONS_CSV, list(rotations.items()))


def _with_status():
    load_csv()
    _load_log('reviews', REVIEWS_CSV)
    _load_log('favorites', FAVORITES_CSV)
    _load_log('rotations', ROTATIONS_CSV)
    _load_log('dates', DATES_CSV)
    _load_log('locations_set', LOCATIONS_SET_CSV)
    _load_deletions()
    return f"""
        (
            SELECT
                exif.* REPLACE (COALESCE(dates.value, exif.DateTimeOriginal) AS DateTimeOriginal),
                dates.value IS NOT NULL AS date_assigned,
                COALESCE(reviews.value, '{UNREVIEWED}') AS review_status,
                COALESCE(favorites.value = '{FAVORITE}', false) AS favorite,
                COALESCE(TRY_CAST(rotations.value AS INTEGER), 0) AS rotation
            FROM exif
            LEFT JOIN reviews USING (photo_id)
            LEFT JOIN favorites USING (photo_id)
            LEFT JOIN rotations USING (photo_id)
            LEFT JOIN dates USING (photo_id)
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


def favorite_photos(limit):
    """
    Approved favorites in a random order, for a frame with nothing of its own to show.  Failing
    any favorites, approved photos from anywhere
    """
    query = f"""
        SELECT SourceFile, rotation FROM {_with_status()}
        WHERE review_status = ? AND (favorite OR ?)
        ORDER BY random()
        LIMIT ?
    """
    rows = db.execute(query, [APPROVED, False, limit]).fetchall() or db.execute(query, [APPROVED, True, limit]).fetchall()
    return [{'SourceFile': path, 'rotation': rotation} for path, rotation in rows]


def recent_photos(recent, limit):
    """
    Photos taken in a recent range, newest first, without waiting for a review: those approved, and
    of the unreviewed ones, those the review queue starts out checked.  Also returns the rest of
    each one's burst, by photo id, since reviewing a burst's photo decides those as well
    """
    tiles = _review_queue_page(RECENT_FILTER, list(recent), 0, limit)['items']
    bursts = {
        tile['id']: [other['id'] for other in tile['burst']]
        for tile in tiles
        if not tile['no_camera']
    }
    photos = db.execute(
        f"""
        SELECT * FROM {_with_status()}
        WHERE {RECENT_FILTER} AND (review_status = ? OR list_contains(?::VARCHAR[], photo_id))
        ORDER BY DateTimeOriginal DESC, photo_id
        LIMIT ?
        """,
        [*recent, APPROVED, list(bursts), limit]
    ).df()
    return photos, bursts


def select_photos(datestring='', status=APPROVED):
    return db.execute(
        f"""
        SELECT * FROM {_with_status()}
        WHERE DateTimeOriginal LIKE ? AND review_status = ?
        """,
        [f"{datestring}%", status]
    ).df()


def _taken_filter(datestring, recent=None):
    # SQL and its parameters for photos on a date and, if there's one, in a recent range
    where = "DateTimeOriginal LIKE ?"
    params = [f"{datestring}%"]
    if recent:
        return f"({where} OR ({RECENT_FILTER}))", [*params, *recent]
    return where, params


def review_counts(datestring='', recent=None):
    where, params = _taken_filter(datestring, recent)
    rows = db.execute(
        f"""
        SELECT review_status, COUNT(*) FROM {_with_status()}
        WHERE {where}
        GROUP BY review_status
        """,
        params
    ).fetchall()
    return {APPROVED: 0, REJECTED: 0, UNREVIEWED: 0, **dict(rows)}


def _library_filter(with_faces):
    if not with_faces:
        return ''
    _load_log('faces', FACES_CSV)
    return f"AND photo_id IN (SELECT photo_id FROM faces WHERE value <> '{NO_FACES}')"


def library_months(with_faces=False):
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
        WHERE review_status <> ? {_library_filter(with_faces)}
        GROUP BY month
        ORDER BY month DESC NULLS LAST
        """,
        [APPROVED, REJECTED]
    ).fetchall()
    return [{'month': month, 'photos': photos, 'cover': cover} for month, photos, cover in rows]


def _library_rows(where, params, hidden=False):
    # hidden photos only appear if asked for
    rows = db.execute(
        f"""
        SELECT photo_id, SourceFile, DateTimeOriginal, review_status, favorite, rotation, date_assigned
        FROM {_with_status()}
        WHERE ({where}) AND (review_status <> ? OR ?)
        ORDER BY DateTimeOriginal DESC, photo_id
        """,
        [*params, REJECTED, hidden]
    ).fetchall()
    return [
        {
            'id': photo_id,
            'path': path,
            'taken': taken,
            'status': status,
            'favorite': favorite,
            'rotation': rotation,
            'date_assigned': date_assigned
        }
        for photo_id, path, taken, status, favorite, rotation, date_assigned in rows
    ]


def library_photos(month, with_faces=False):
    # month as in library_months, where None is the undated photos
    return _library_rows(f"({LIBRARY_MONTH}) IS NOT DISTINCT FROM ? {_library_filter(with_faces)}", [month])


def photos_with_paths(paths):
    return _library_rows("SourceFile IN (SELECT UNNEST(?::VARCHAR[]))", [list(paths)])


def favorites():
    # not hidden ones, as hiding a photo takes it out of the library
    return _library_rows("favorite", [])


def hidden_photos():
    """
    Photos someone has hidden, from the library or the review queue, newest first.  Not copies
    hidden automatically as duplicates, which the duplicates page has
    """
    _load_log('duplicates', DUPLICATES_CSV)
    return _library_rows(
        "review_status = ? AND photo_id NOT IN (SELECT photo_id FROM duplicates WHERE value = ?)",
        [REJECTED, HIDDEN_DUPLICATE],
        hidden=True
    )


def rotations_by_path(paths):
    """
    How each of these files was turned, by path, including files deleted since the index was
    loaded, such as those in the trash.  Those are only known until a scan leaves them out
    """
    _with_status()
    return dict(db.execute(
        """
        SELECT SourceFile, TRY_CAST(rotations.value AS INTEGER)
        FROM photo_files JOIN rotations USING (photo_id)
        WHERE SourceFile IN (SELECT UNNEST(?::VARCHAR[]))
        """,
        [list(paths)]
    ).fetchall())


def deletion_times():
    # when each path was last deleted, in seconds since the epoch
    if not os.path.exists(DELETIONS_CSV):
        return {}
    return dict(db.execute(
        """
        SELECT photo_id, MAX(decided_ns) / 1e9
        FROM read_csv(
            ?,
            header=false,
            delim=',',
            quote='"',
            escape='"',
            columns={'photo_id': 'VARCHAR', 'value': 'VARCHAR', 'decided_ns': 'BIGINT'},
            ignore_errors=true
        )
        WHERE value = ?
        GROUP BY photo_id
        """,
        [DELETIONS_CSV, DELETED]
    ).fetchall())


def indexed(paths):
    # whether any of these files is in the index, deleted or not
    load_csv()
    return db.execute(
        "SELECT COUNT(*) > 0 FROM photo_files WHERE SourceFile IN (SELECT UNNEST(?::VARCHAR[]))",
        [list(paths)]
    ).fetchone()[0]


def shown_paths():
    table = _with_status()
    return [row[0] for row in db.execute(f"SELECT SourceFile FROM {table} WHERE review_status <> ?", [REJECTED]).fetchall()]


def photo_details(path):
    table = _with_status()
    cursor = db.execute(f"SELECT * FROM {table} WHERE SourceFile = ?", [path])
    row = cursor.fetchone()
    return dict(zip((column[0] for column in cursor.description), row)) if row else None


def photos_with_ids(photo_ids, hidden=False):
    return _library_rows("photo_id IN (SELECT UNNEST(?::VARCHAR[]))", [photo_ids], hidden)


def copies_of(path):
    """
    Every file in the index that's the same photo as this one, including this one, such as
    the same screenshot backed up into two folders
    """
    load_csv()
    rows = db.execute(
        "SELECT SourceFile FROM photo_files WHERE photo_id IN (SELECT photo_id FROM photo_files WHERE SourceFile = ?)",
        [path]
    ).fetchall()
    return [found for found, in rows] or [path]


def screenshots():
    """
    Screenshots from an iPhone or iPad, newest first, which iOS marks as such.  None for an index
    from before scans read that mark, until a scan has read every photo again
    """
    if 'Screenshot' not in tags():
        return None
    # a column with nothing in it yet reads as text rather than a number
    return _library_rows("CAST(Screenshot AS VARCHAR) = '1'", [])


def dated_folders():
    # each folder with a month its photos are from, going by the dates in the files themselves
    return set(db.execute(
        f"""
        SELECT DISTINCT REGEXP_REPLACE(SourceFile, '/[^/]*$', ''), {LIBRARY_MONTH}
        FROM {_with_status()}
        WHERE NOT date_assigned AND ({LIBRARY_MONTH}) IS NOT NULL
        """
    ).fetchall())


def assign_date(photo_ids, date):
    """
    Give photos a date, keeping their order.  Their times are only there to keep that order, and
    to keep them apart, since photos close together in time get reviewed as a burst.  Returns
    what each photo's date was before, as restore_dates takes it, with '' for its own date
    """
    _with_status()
    rows = db.execute(
        """
        SELECT photo_id, COALESCE(dates.value, '')
        FROM exif LEFT JOIN dates USING (photo_id)
        WHERE photo_id IN (SELECT UNNEST(?::VARCHAR[]))
        ORDER BY exif.DateTimeOriginal, photo_id
        """,
        [photo_ids]
    ).fetchall()
    midnight = datetime.datetime.combine(date, datetime.time())
    gap = datetime.timedelta(seconds=BURST_GAP_SECS + 1)
    per_day = datetime.timedelta(days=1) // gap
    _append_log(DATES_CSV, [
        (photo_id, (midnight + gap * (position % per_day)).strftime('%Y:%m:%d %H:%M:%S'))
        for position, (photo_id, _) in enumerate(rows)
    ])
    return dict(rows)


def restore_dates(previous):
    _append_log(DATES_CSV, list(previous.items()))


def _assigned(table, photo_ids):
    # what each of these photos has been given by hand in a log, with '' for nothing
    _with_status()
    return dict(db.execute(
        f"""
        SELECT photo_id, COALESCE({table}.value, '')
        FROM exif LEFT JOIN {table} USING (photo_id)
        WHERE photo_id IN (SELECT UNNEST(?::VARCHAR[]))
        """,
        [list(photo_ids)]
    ).fetchall())


def set_taken(photo_id, taken):
    """
    Gives one photo an exact date and time.  Returns what it was before, as restore_dates takes it
    """
    previous = _assigned('dates', [photo_id])
    _append_log(DATES_CSV, [(photo_id, taken.strftime('%Y:%m:%d %H:%M:%S'))])
    return previous


def own_dates(photo_ids):
    """
    Takes photos back to the dates in their files.  Returns what those given one by hand had, as
    restore_dates takes it
    """
    previous = {photo_id: value for photo_id, value in _assigned('dates', photo_ids).items() if value}
    _append_log(DATES_CSV, [(photo_id, '') for photo_id in previous])
    return previous


def own_taken(photo_ids):
    # each photo's date as it's now used, by photo id
    return dict(db.execute(
        f"SELECT photo_id, DateTimeOriginal FROM {_with_status()} WHERE photo_id IN (SELECT UNNEST(?::VARCHAR[]))",
        [list(photo_ids)]
    ).fetchall())


def own_date(photo_id):
    # the date in the photo's file, whatever it's been given since
    load_csv()
    row = db.execute("SELECT DateTimeOriginal FROM exif WHERE photo_id = ?", [photo_id]).fetchone()
    return row[0] if row and row[0] != NO_DATE else None


def set_locations(photo_ids, value):
    """
    Says where photos were taken, as "<lat> <lon>", NO_LOCATION, or '' for what their files say.
    Returns what each had before, for restore_locations
    """
    previous = _assigned('locations_set', photo_ids)
    _append_log(LOCATIONS_SET_CSV, [(photo_id, value) for photo_id in previous])
    return previous


def restore_locations(previous):
    _append_log(LOCATIONS_SET_CSV, list(previous.items()))


def locations_set():
    """
    Where photos were said to be taken by hand, by the path of each one's file in the library
    """
    _with_status()
    return dict(db.execute(
        "SELECT SourceFile, locations_set.value FROM exif JOIN locations_set USING (photo_id) WHERE locations_set.value IS NOT NULL"
    ).fetchall())


def cameras():
    """
    Every camera with photos in the library, as its make and model, how many photos it took, the
    years of its first and last, and one to show.  Those with the most photos first, as the
    library's own cameras are, rather than those of friends who sent a photo or two.  Photos with no
    camera, such as screenshots and scans, go last
    """
    rows = db.execute(
        f"""
        SELECT
            {CAMERA_MAKE} AS make, {CAMERA_MODEL} AS model, COUNT(*),
            MIN({LIBRARY_MONTH}), MAX({LIBRARY_MONTH}),
            first(struct_pack(path := SourceFile, rotation := rotation) ORDER BY
                favorite DESC, review_status = ? DESC, DateTimeOriginal DESC
            )
        FROM {_with_status()}
        WHERE review_status <> ?
        GROUP BY make, model
        ORDER BY make IS NULL AND model IS NULL, COUNT(*) DESC, MAX({LIBRARY_MONTH}) DESC NULLS LAST
        """,
        [APPROVED, REJECTED]
    ).fetchall()
    return [
        {
            'make': make,
            'model': model,
            'photos': count,
            'first': first[:4] if first else None,
            'last': last[:4] if last else None,
            'cover': cover
        }
        for make, model, count, first, last, cover in rows
    ]


def camera_photos(make, model):
    return _library_rows(f"{CAMERA_MAKE} IS NOT DISTINCT FROM ? AND {CAMERA_MODEL} IS NOT DISTINCT FROM ?", [make, model])


def face_counts():
    # among photos in the index, how many have been checked for faces and how many have some
    table = _with_status()
    _load_log('faces', FACES_CSV)
    checked, with_faces = db.execute(
        f"""
        SELECT COUNT(*), COUNT(*) FILTER (WHERE value <> ?)
        FROM {table} JOIN faces USING (photo_id)
        """,
        [NO_FACES]
    ).fetchone()
    return {'checked': checked, 'with_faces': with_faces}


def photos_to_check_for_faces():
    # newest first, as those are the ones most likely to be looked at while this runs
    table = _with_status()
    _load_log('faces', FACES_CSV)
    rows = db.execute(
        f"""
        SELECT photo_id, SourceFile, rotation
        FROM {table}
        WHERE photo_id NOT IN (SELECT photo_id FROM faces)
        ORDER BY DateTimeOriginal DESC
        """
    ).fetchall()
    return [{'id': photo_id, 'path': path, 'rotation': rotation} for photo_id, path, rotation in rows]


def photos_with_faces():
    # those the check found faces in, newest first, as those are the most likely to be looked at
    table = _with_status()
    _load_log('faces', FACES_CSV)
    rows = db.execute(
        f"""
        SELECT photo_id, SourceFile, rotation
        FROM {table} JOIN faces USING (photo_id)
        WHERE value <> ? AND review_status <> ?
        ORDER BY DateTimeOriginal DESC
        """,
        [NO_FACES, REJECTED]
    ).fetchall()
    return [{'id': photo_id, 'path': path, 'rotation': rotation} for photo_id, path, rotation in rows]


def shown_photo_list():
    # every photo in the library that isn't hidden, newest first
    table = _with_status()
    rows = db.execute(
        f"""
        SELECT photo_id, SourceFile, rotation FROM {table}
        WHERE review_status <> ?
        ORDER BY DateTimeOriginal DESC
        """,
        [REJECTED]
    ).fetchall()
    return [{'id': photo_id, 'path': path, 'rotation': rotation} for photo_id, path, rotation in rows]


def faces_stamp():
    # the same until the check for faces has found more
    return _file_stamps(FACES_CSV)


def shown_photos():
    """
    What's in the library and not hidden, and something that stays the same until that changes
    """
    table = _with_status()
    stamp = _file_stamps(EXIF_CSV, REVIEWS_CSV, DELETIONS_CSV)
    return stamp, {row[0] for row in db.execute(f"SELECT photo_id FROM {table} WHERE review_status <> ?", [REJECTED]).fetchall()}


def duplicate_photos(photo_ids=None):
    # what deciding between copies of a photo goes on, for these photos or all of them
    table = _with_status()
    rows = db.execute(
        f"""
        SELECT
            photo_id, SourceFile, FileName, FileType,
            TRY_CAST(FileSize AS BIGINT), TRY_CAST(ImageWidth AS BIGINT), TRY_CAST(ImageHeight AS BIGINT),
            Make IS NOT NULL OR Model IS NOT NULL,
            ({LIBRARY_MONTH}) IS NOT NULL,
            date_assigned,
            DateTimeOriginal, review_status, favorite, rotation
        FROM {table}
        WHERE ? OR photo_id IN (SELECT UNNEST(?::VARCHAR[]))
        """,
        [photo_ids is None, list(photo_ids or ())]
    ).fetchall()
    return [
        {
            'id': photo_id,
            'path': path,
            'name': name,
            'type': file_type,
            'size': size,
            'width': width,
            'height': height,
            'camera': camera,
            'dated': dated,
            # when it was taken, by its own account
            'taken': taken if dated and not assigned else None,
            'date': taken,
            'status': status,
            'favorite': favorite,
            'rotation': rotation
        }
        for photo_id, path, name, file_type, size, width, height, camera, dated, assigned, taken, status, favorite, rotation
        in rows
    ]


def handled_duplicates():
    _load_log('duplicates', DUPLICATES_CSV)
    return dict(db.execute("SELECT photo_id, value FROM duplicates").fetchall())


def record_duplicates(rows):
    _append_log(DUPLICATES_CSV, rows)


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


def review_page(datestring, status, offset, limit, recent=None):
    where, params = _taken_filter(datestring, recent)
    if status == UNREVIEWED:
        return _review_queue_page(where, params, offset, limit)
    table = _with_status()
    statuses = [APPROVED, REJECTED] if status == REVIEWED else [status]
    filters = [*params, statuses]
    total = db.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {where} AND list_contains(?, review_status)",
        filters
    ).fetchone()[0]
    # reviewing shrinks the queue, which can leave the offset past its end
    offset = min(offset, max(total - limit, 0))
    rows = db.execute(
        f"""
        SELECT photo_id, SourceFile, DateTimeOriginal, review_status, rotation FROM {table}
        WHERE {where} AND list_contains(?, review_status)
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


def _review_queue_page(where, params, offset, limit):
    """
    A page of the queue, which counts bursts of photos as one tile each.  Each tile's item is
    the middle photo of its burst, with the others listed in its 'burst'
    """
    table = _with_status()
    _load_log('fingerprints', FINGERPRINTS_CSV)
    tiles = f"""
        WITH queue AS (
            SELECT
                photos.photo_id, SourceFile, DateTimeOriginal, review_status, rotation, Make, Model,
                TRY_STRPTIME(LEFT(DateTimeOriginal, 19), '%Y:%m:%d %H:%M:%S') AS taken,
                -- the first of a fingerprint's two hashes covers the whole photo
                TRY_CAST('0x' || LEFT(fingerprints.value, 16) AS UBIGINT) AS fingerprint
            FROM {table} AS photos
            LEFT JOIN fingerprints ON fingerprints.photo_id = SourceFile
            WHERE {where} AND review_status = '{UNREVIEWED}'
        ), bursts AS (
            -- with no camera, time or fingerprint to go on, a photo is on its own
            SELECT *, SUM((
                Model IS NULL OR taken IS NULL
                OR COALESCE(date_diff('second', previous_taken, taken) > {BURST_GAP_SECS}, true)
                OR COALESCE(bit_count(xor(previous_fingerprint, fingerprint)) > {BURST_MAX_DIFFERENCE}, true)
            )::INT) OVER (PARTITION BY Model ORDER BY taken, photo_id ROWS UNBOUNDED PRECEDING) AS burst
            FROM (
                SELECT
                    *,
                    LAG(taken) OVER (PARTITION BY Model ORDER BY taken, photo_id) AS previous_taken,
                    LAG(fingerprint) OVER (PARTITION BY Model ORDER BY taken, photo_id) AS previous_fingerprint
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
            ORDER BY tile, position DESC
            """,
            [*params, start, start + limit]
        ).fetchall()

    rows = fetch(offset)
    if rows:
        total, photos = rows[0][:2]
    else:
        total, photos = db.execute(
            f"{tiles} SELECT COALESCE(MAX(tile), 0), COUNT(*) FROM numbered",
            params
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
