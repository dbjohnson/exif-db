import os
import datetime
import glob

import duckdb


LIBRARY_ROOT = '/photo'
EXIF_CSV = '/storage/exif.csv'
db = duckdb.connect(database=':memory:', read_only=False)


def load_csv(force_reload=False):
    # Load the CSV data into a DuckDB table, and keep only the last row for each file
    if force_reload or update_available():
        db.execute(
            """
            CREATE OR REPLACE TABLE exif AS
            SELECT *, ?::TIMESTAMP AS last_modified
            FROM read_csv_auto(?, sample_size=-1, ignore_errors=true)
            WHERE MIMEType LIKE 'image/%'
            QUALIFY
            ROW_NUMBER() OVER (PARTITION BY SourceFile ORDER BY FileAccessDate DESC) = 1;
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
    return db.execute(query).df()


def tags():
    return db.execute("PRAGMA table_info('exif');").fetchdf()['name'].tolist()


def prune_deleted():
    """
    Remove rows for files that have been deleted or moved
    """
    db.execute(
        f"""
        COPY (
            SELECT * FROM exif
            WHERE SourceFile IN ?
        )
        TO '{EXIF_CSV}' (HEADER, DELIMITER ',')
        """,
        [glob.glob(f"{LIBRARY_ROOT}/**/*", recursive=True)]
    )
    load_csv()


def select_by_date(datestring):
    return db.execute(
        f"""
        SELECT * FROM exif
        WHERE DateTimeOriginal LIKE '{datestring}%'
        """,
    ).df()
