"""
Where faces, the names given to them and each person's cover are kept: a SQLite file in the storage
volume, shared by the web app and the worker that finds faces.  Imports nothing from the api
package, since workers load this by itself without starting a web app.
"""
import hashlib
import os
import sqlite3
import time

import numpy as np

PATH = os.getenv('PEOPLE_DB', '/storage/people.db')
EMBEDDING_SIZE = 512
# below these, a face isn't good enough to compare or to offer for naming, from a test on photos
# from 2025 where most of those below were blurs, the backs of heads and things that aren't faces
MIN_SCORE = 0.7
MIN_PIXELS = 30
# how alike two faces have to be, out of 1, to start out in the same group, and how many of a new
# face's closest faces get a say in which group it goes in.  It's easier to name several groups for
# a person than to untangle two people in one
CLUSTER_SIMILARITY = 0.5
CLUSTER_NEIGHBOURS = 10
SCHEMA = """
    CREATE TABLE IF NOT EXISTS faces (
        id TEXT PRIMARY KEY,
        -- as bytes, since a file's name on disk needn't be valid text
        photo_id BLOB NOT NULL,
        path BLOB NOT NULL,
        -- how far the copy this was found in had been turned, which the box is a fraction of
        rotation INTEGER NOT NULL,
        left REAL NOT NULL,
        top REAL NOT NULL,
        width REAL NOT NULL,
        height REAL NOT NULL,
        score REAL NOT NULL,
        pixels REAL NOT NULL,
        norm REAL NOT NULL,
        embedding BLOB NOT NULL,
        -- which faces look alike; null for one too poor to compare
        cluster INTEGER
    );
    CREATE INDEX IF NOT EXISTS faces_photo ON faces (photo_id);
    CREATE INDEX IF NOT EXISTS faces_cluster ON faces (cluster);
    -- photos that have been looked at, including those with no faces
    CREATE TABLE IF NOT EXISTS scanned (photo_id BLOB PRIMARY KEY, faces INTEGER NOT NULL, at INTEGER NOT NULL);
    -- who a face is; '' for one looked at and not worth naming.  A face with no row is undecided
    CREATE TABLE IF NOT EXISTS names (face_id TEXT PRIMARY KEY, name TEXT NOT NULL, at INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS names_name ON names (name);
    -- every change to a name, in case one needs working out afterwards
    CREATE TABLE IF NOT EXISTS name_history (face_id TEXT NOT NULL, name TEXT, at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS covers (name TEXT PRIMARY KEY, face_id TEXT NOT NULL);
    -- faces someone has looked at because they seemed to be someone else, and settled
    CREATE TABLE IF NOT EXISTS checked_faces (face_id TEXT PRIMARY KEY, at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    -- photos not to show faces from, as hidden and deleted ones are only known to the main index
    CREATE TABLE IF NOT EXISTS hidden_photos (photo_id BLOB PRIMARY KEY);
"""


def connect(path=None):
    # long enough to wait out the worker, which only ever holds the file for one small write
    connection = sqlite3.connect(path or PATH, timeout=60)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA synchronous=NORMAL')
    connection.executescript(SCHEMA)
    return connection


def to_db(text):
    return text.encode('utf-8', 'surrogateescape')


def from_db(value):
    return value.decode('utf-8', 'surrogateescape')


def face_id(photo_id, face):
    # the same photo and box always gets the same id, so that finding it again changes nothing
    box = f"{face['left']:.5f} {face['top']:.5f} {face['width']:.5f} {face['height']:.5f}"
    return hashlib.sha1(to_db(photo_id) + box.encode()).hexdigest()[:32]


def embedding_from_db(value):
    return np.frombuffer(value, dtype=np.float32)


def save_photo(connection, photo, found):
    """
    A photo's faces and that it's been looked at, together, so that a crash can't leave half of it
    """
    with connection:
        connection.executemany(
            """
            INSERT OR IGNORE INTO faces
                (id, photo_id, path, rotation, left, top, width, height, score, pixels, norm, embedding)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    face_id(photo['id'], face),
                    to_db(photo['id']),
                    to_db(photo['path']),
                    photo['rotation'],
                    face['left'],
                    face['top'],
                    face['width'],
                    face['height'],
                    face['score'],
                    face['pixels'],
                    face['norm'],
                    face['embedding'].tobytes()
                )
                for face in found
            ]
        )
        connection.execute(
            'INSERT OR REPLACE INTO scanned (photo_id, faces, at) VALUES (?, ?, ?)',
            (to_db(photo['id']), len(found), int(time.time()))
        )


def scanned_photos(connection):
    return {from_db(row[0]) for row in connection.execute('SELECT photo_id FROM scanned')}


def _usable_where():
    return f"score >= {MIN_SCORE} AND pixels >= {MIN_PIXELS}"


def attach_new_faces(connection):
    """
    Puts each new face in the group its closest faces are mostly in, or a group of its own.  Groups
    are worked out for the whole library once, by scripts/cluster_people.py, and only added to here:
    that's slow enough on a NAS that it can't be done again every time something changes
    """
    new = connection.execute(
        f'SELECT id, embedding FROM faces WHERE cluster IS NULL AND {_usable_where()}'
    ).fetchall()
    if not new:
        return 0
    rows = connection.execute(
        'SELECT cluster, embedding FROM faces WHERE cluster IS NOT NULL'
    ).fetchall()
    count = len(rows)
    known = np.empty((count + len(new), EMBEDDING_SIZE), dtype=np.float32)
    clusters = np.empty(count + len(new), dtype=np.int64)
    for place, (cluster, embedding) in enumerate(rows):
        known[place] = embedding_from_db(embedding)
        clusters[place] = cluster
    next_cluster = int(clusters[:count].max()) + 1 if count else 1
    chosen = []
    for face_id, embedding in new:
        vector = embedding_from_db(embedding)
        similar = known[:count] @ vector
        nearest = np.argpartition(-similar, min(CLUSTER_NEIGHBOURS, count - 1))[:CLUSTER_NEIGHBOURS] if count else []
        votes = {}
        for place in nearest:
            if similar[place] >= CLUSTER_SIMILARITY:
                votes[int(clusters[place])] = votes.get(int(clusters[place]), 0) + float(similar[place])
        if votes:
            cluster = max(votes, key=votes.get)
        else:
            cluster = next_cluster
            next_cluster += 1
        known[count] = vector
        clusters[count] = cluster
        count += 1
        chosen.append((cluster, face_id))
    with connection:
        connection.executemany('UPDATE faces SET cluster = ? WHERE id = ?', chosen)
    return len(chosen)
