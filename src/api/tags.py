"""
Tags given to photos by hand, each of which the library shows as a folder.  Kept in a SQLite file in
the storage volume, by photo id, so that a tag stays with a photo whichever copy of it is kept.
"""
import os
import sqlite3
import threading
import time

from api.people_db import from_db, to_db

PATH = os.getenv('TAGS_DB', '/storage/tags.db')
SCHEMA = """
    -- every tag, including those with no photos yet
    CREATE TABLE IF NOT EXISTS tags (name TEXT PRIMARY KEY, at INTEGER NOT NULL);
    -- photo ids as bytes, since a file's name on disk needn't be valid text
    CREATE TABLE IF NOT EXISTS photo_tags (
        photo_id BLOB NOT NULL,
        tag TEXT NOT NULL,
        at INTEGER NOT NULL,
        PRIMARY KEY (photo_id, tag)
    );
    CREATE INDEX IF NOT EXISTS photo_tags_tag ON photo_tags (tag);
"""
# there's a limit on how many values one query can be given
BATCH = 500
_local = threading.local()


def _db():
    if getattr(_local, 'connection', None) is None:
        connection = sqlite3.connect(PATH, timeout=60)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.executescript(SCHEMA)
        _local.connection = connection
    return _local.connection


def names():
    return [row[0] for row in _db().execute('SELECT name FROM tags ORDER BY name COLLATE NOCASE')]


def exists(name):
    return _db().execute('SELECT 1 FROM tags WHERE name = ?', (name,)).fetchone() is not None


def create(name):
    with _db() as db:
        db.execute('INSERT OR IGNORE INTO tags (name, at) VALUES (?, ?)', (name, time.time_ns()))


def photo_ids(name):
    return [from_db(row[0]) for row in _db().execute('SELECT photo_id FROM photo_tags WHERE tag = ?', (name,))]


def of_photo(photo_id):
    return [row[0] for row in _db().execute(
        'SELECT tag FROM photo_tags WHERE photo_id = ? ORDER BY tag COLLATE NOCASE',
        (to_db(photo_id),)
    )]


def _tagged(name, ids):
    # those of these photos that have this tag
    found = set()
    for start in range(0, len(ids), BATCH):
        part = [to_db(photo_id) for photo_id in ids[start:start + BATCH]]
        marks = ','.join('?' * len(part))
        found.update(from_db(row[0]) for row in _db().execute(
            f'SELECT photo_id FROM photo_tags WHERE tag = ? AND photo_id IN ({marks})',
            (name, *part)
        ))
    return found


def set_photos(name, ids, tagged):
    """
    Adds this tag to these photos, or takes it off, creating the tag if it's new.  Returns the photos
    that changed, which doing the opposite undoes
    """
    ids = list(dict.fromkeys(ids))
    already = _tagged(name, ids)
    changed = [photo_id for photo_id in ids if (photo_id in already) != tagged]
    now = time.time_ns()
    with _db() as db:
        db.execute('INSERT OR IGNORE INTO tags (name, at) VALUES (?, ?)', (name, now))
        if tagged:
            db.executemany(
                'INSERT INTO photo_tags (photo_id, tag, at) VALUES (?, ?, ?)',
                [(to_db(photo_id), name, now) for photo_id in changed]
            )
        else:
            db.executemany(
                'DELETE FROM photo_tags WHERE photo_id = ? AND tag = ?',
                [(to_db(photo_id), name) for photo_id in changed]
            )
    return changed


def _state(name):
    # what restore needs to put a tag back as it is now: its photos, or None if there's no such tag
    return {'name': name, 'photo_ids': photo_ids(name) if exists(name) else None}


def restore(states):
    """
    Puts tags back the way _state found them, in order
    """
    now = time.time_ns()
    with _db() as db:
        for state in states:
            db.execute('DELETE FROM photo_tags WHERE tag = ?', (state['name'],))
            if state['photo_ids'] is None:
                db.execute('DELETE FROM tags WHERE name = ?', (state['name'],))
                continue
            db.execute('INSERT OR IGNORE INTO tags (name, at) VALUES (?, ?)', (state['name'], now))
            db.executemany(
                'INSERT OR IGNORE INTO photo_tags (photo_id, tag, at) VALUES (?, ?, ?)',
                [(to_db(photo_id), state['name'], now) for photo_id in state['photo_ids']]
            )


def rename(old, new):
    """
    Gives a tag's photos the tag new instead, which joins the two if new is already a tag.  Returns
    how both were before, for restore, or None if there's no tag called old
    """
    if not exists(old):
        return None
    before = [_state(new), _state(old)]
    if old == new:
        return before
    now = time.time_ns()
    with _db() as db:
        db.execute('INSERT OR IGNORE INTO tags (name, at) VALUES (?, ?)', (new, now))
        db.execute('UPDATE OR IGNORE photo_tags SET tag = ? WHERE tag = ?', (new, old))
        # those that had both
        db.execute('DELETE FROM photo_tags WHERE tag = ?', (old,))
        db.execute('DELETE FROM tags WHERE name = ?', (old,))
    return before


def delete(name):
    """
    Takes a tag off every photo and away, leaving the photos alone.  Returns how it was before, for
    restore, or None if there's no such tag
    """
    if not exists(name):
        return None
    before = [_state(name)]
    restore([{'name': name, 'photo_ids': None}])
    return before
