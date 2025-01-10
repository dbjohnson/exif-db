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
            SELECT *, ?::TIMESTAMP last_modified
            FROM read_csv_auto(?, sample_size=-1, ignore_errors=true)
            WHERE MIMEType LIKE 'image/%'
            AND DateTimeOriginal IS NOT NULL
            -- prefer JPEGs, then PNGs, then HEICs, then everything else
            QUALIFY ROW_NUMBER() OVER (
               PARTITION BY LOWER(REGEXP_REPLACE(FileName, '\\.[^.]*$', ''))
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


def delete_image(path):
    load_csv()
    db.execute(
        """
        COPY (
            SELECT {} FROM exif
            WHERE SourceFile <> ?
        )
        TO '{}' (HEADER, DELIMITER ',')
        """.format(
            ",".join(tags()),
            EXIF_CSV
        ),
        [path]
    )


def prune_deleted():
    """
    Remove rows for files that have been deleted or moved
    """
    load_csv()
    db.execute(
        """
        COPY (
            SELECT {} FROM exif
            WHERE SourceFile IN ?
        )
        TO '{}' (HEADER, DELIMITER ',')
        """.format(
            ",".join(tags()),
            EXIF_CSV
        ),
        [glob.glob(f"{LIBRARY_ROOT}/**/*", recursive=True)]
    )
    load_csv()


def select_by_date(datestring):
    load_csv()
    return db.execute(
        f"""
        SELECT * FROM exif
        WHERE DateTimeOriginal LIKE '{datestring}%'
        """,
    ).df()
