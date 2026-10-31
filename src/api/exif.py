import csv
import fcntl
import os
import datetime
import glob

import duckdb


LIBRARY_ROOT = '/photo'
EXIF_CSV = '/storage/exif.csv'
db = duckdb.connect(database=':memory:', read_only=False)


def load_csv(force_reload=False):
    # Load the CSV data into a DuckDB table, and keep only one row for each photo.
    # Copies / alternate formats of a photo share a base name and capture time;
    # base names alone are reused by cameras and phones
    if force_reload or update_available():
        db.execute(
            """
            CREATE OR REPLACE TABLE exif AS
            SELECT *, ?::TIMESTAMP last_modified
            FROM read_csv_auto(?, sample_size=-1, ignore_errors=true)
            WHERE MIMEType LIKE 'image/%'
            AND DateTimeOriginal IS NOT NULL
            -- prefer JPEGs, then PNGs, then HEICs, then everything else
            QUALIFY ROW_NUMBER() OVER (
               PARTITION BY LOWER(REGEXP_REPLACE(FileName, '\\.[^.]*$', '')), DateTimeOriginal
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
    return (
        db.execute("SHOW TABLES").fetchall() == []
        or last_modified() > db.execute("SELECT MAX(last_modified) FROM exif").fetchone()[0]
    )


def dataframe(query='select * from exif'):
    load_csv()
    return db.execute(query).df()


def tags():
    load_csv()
    return [
        col
        for col in db.execute("PRAGMA table_info('exif');").fetchdf()['name'].tolist()
        if col != 'last_modified'
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


def select_by_date(datestring):
    load_csv()
    return db.execute(
        f"""
        SELECT * FROM exif
        WHERE DateTimeOriginal LIKE '{datestring}%'
        """,
    ).df()
