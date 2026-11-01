"""
Who's in the photos: the faces scripts/find_people.py finds, which look alike, and the names given to
them.  It's all in people.db, which see people_db.py for.
"""
import csv
import json
import logging
import os
import threading
import time
from collections import Counter

import numpy as np

from api import people_db, thumbs
from api.jobs import Job
from api.people_db import from_db, to_db

logger = logging.getLogger('uvicorn.error')
job = Job('people', 'find_people.py')
# a face that's been looked at and isn't anyone worth naming
NOBODY = ''
# below these, a face isn't good enough to compare or to offer for naming: they're mostly blurs,
# the backs of heads and things that aren't faces at all, which all look alike to the model
MIN_SCORE = people_db.MIN_SCORE
MIN_PIXELS = people_db.MIN_PIXELS
USABLE = f"(score >= {MIN_SCORE} AND pixels >= {MIN_PIXELS})"
# how alike a group's faces have to be to a named person's, out of 1, to suggest that name
SUGGEST_SIMILARITY = 0.45
# of each person's faces to compare with: their best, as a person can have tens of thousands
SUGGEST_FACES_PER_PERSON = 200
SUGGEST_NEIGHBOURS = 5
PAGE_FACES = 500
# what Rekognition's results left behind, for the names given then to be carried over
OLD_FACES = '/storage/rekognition/indexed.jsonl'
OLD_NAMES_CSV = '/storage/people.csv'
OLD_COVERS_CSV = '/storage/people_covers.csv'
OLD_UNDECIDED = '\x7fundecided'
OLD_MATCH_OVERLAP = 0.3
_local = threading.local()
_cache = {}


def _db():
    if getattr(_local, 'connection', None) is None:
        _local.connection = people_db.connect()
    return _local.connection


def available():
    return os.path.exists(people_db.PATH)


def _stamp():
    # changes whenever a face, a name or which photos are shown does; as hidden_stamp only lives in
    # this process, another's changes show up by the row counts of the tables
    db = _db()
    return (
        db.execute('SELECT MAX(rowid) FROM faces').fetchone()[0],
        db.execute('SELECT MAX(rowid) FROM name_history').fetchone()[0],
        _cache.get('hidden_stamp')
    )


def set_hidden_photos(stamp, shown):
    """
    Everything with faces that isn't among the photos being shown has been hidden or deleted.  Only
    does anything when the stamp has changed, so it's cheap to call before every read
    """
    if _cache.get('hidden_stamp') == stamp:
        return
    db = _db()
    # no index to go on, such as before the first scan, isn't the same as everything being gone
    hidden = people_db.scanned_photos(db) - shown if shown else set()
    with db:
        db.execute('DELETE FROM hidden_photos')
        db.executemany('INSERT INTO hidden_photos (photo_id) VALUES (?)', [(to_db(photo_id),) for photo_id in hidden])
    _cache['hidden_stamp'] = stamp


SHOWN = 'photo_id NOT IN (SELECT photo_id FROM hidden_photos)'


def names():
    return dict(_db().execute('SELECT face_id, name FROM names'))


def _record_names(rows):
    """
    Sets faces' names, given as (face id, name), with None for going back to undecided
    """
    now = time.time_ns()
    db = _db()
    with db:
        db.executemany(
            'INSERT OR REPLACE INTO names (face_id, name, at) VALUES (?, ?, ?)',
            [(face_id, name, now) for face_id, name in rows if name is not None]
        )
        db.executemany('DELETE FROM names WHERE face_id = ?', [(face_id,) for face_id, name in rows if name is None])
        db.executemany('INSERT INTO name_history (face_id, name, at) VALUES (?, ?, ?)', [(face_id, name, now) for face_id, name in rows])


def _current_names(face_ids):
    """
    Each of these that's a known face, with what it's called, or None if it hasn't been decided on
    """
    found = {}
    db = _db()
    # a few at a time, since there's a limit on how many values one query can be given
    for start in range(0, len(face_ids), 500):
        part = face_ids[start:start + 500]
        marks = ','.join('?' * len(part))
        found.update(db.execute(
            f"""
            SELECT faces.id, names.name FROM faces LEFT JOIN names ON names.face_id = faces.id
            WHERE faces.id IN ({marks})
            """,
            part
        ))
    return found


def set_names(face_ids, name):
    """
    Returns what each known face was called before, for putting back with restore_names
    """
    previous = _current_names(face_ids)
    _record_names([(face_id, name) for face_id in previous])
    return previous


def restore_names(previous):
    # None for a face that hadn't been decided on
    known = _current_names(list(previous))
    _record_names([(face_id, previous[face_id]) for face_id in known])


def clear_names(face_ids):
    """
    Takes faces back to undecided, so that they return to their groups.  Returns what each was
    called before, for restore_names
    """
    previous = {face_id: name for face_id, name in _current_names(face_ids).items() if name is not None}
    _record_names([(face_id, None) for face_id in previous])
    return previous


def rename(old, new):
    """
    Gives everyone called old the name new, which joins them up if new is already someone.  Returns
    what each face was called before, or None if there's nobody called old
    """
    members = _named(old)
    if not members or old == new:
        return None
    # this person's chosen cover goes along, unless the person they're joining has one
    cover_face = chosen_cover(old)
    joining_with_cover = chosen_cover(new) is not None
    _record_names([(face_id, new) for face_id in members])
    if cover_face and not joining_with_cover:
        with _db() as db:
            db.execute('INSERT OR REPLACE INTO covers (name, face_id) VALUES (?, ?)', (new, cover_face))
    return {face_id: old for face_id in members}


def remove(name):
    """
    Takes a person away, with their faces going back to undecided.  Returns what each face was
    called before, or None if there's nobody by that name
    """
    members = _named(name)
    if not members:
        return None
    return clear_names(members)


def _named(name):
    return [row[0] for row in _db().execute('SELECT face_id FROM names WHERE name = ? AND name <> ?', (name, NOBODY))]


def chosen_cover(name):
    row = _db().execute(
        """
        SELECT covers.face_id FROM covers JOIN names ON names.face_id = covers.face_id AND names.name = covers.name
        WHERE covers.name = ?
        """,
        (name,)
    ).fetchone()
    return row[0] if row else None


def set_cover(name, face_id):
    """
    Chooses the face standing for a person, or None to go back to automatic.  Returns the one chosen
    before, for putting back, or False if that face isn't this person's
    """
    if face_id is not None and _current_names([face_id]).get(face_id) != name:
        return False
    previous = chosen_cover(name)
    with _db() as db:
        if face_id is None:
            db.execute('DELETE FROM covers WHERE name = ?', (name,))
        else:
            db.execute('INSERT OR REPLACE INTO covers (name, face_id) VALUES (?, ?)', (name, face_id))
    return previous


def photo_faces(photo_id):
    """
    The faces found in a photo, where they are and who they're said to be.  name is None for a face
    that hasn't been decided on, and NOBODY for one that was skipped.  A box is a fraction of the
    copy of the photo that was looked at, which was turned by rotation: the photo may have been
    turned since
    """
    rows = _db().execute(
        f"""
        SELECT faces.id, left, top, width, height, rotation, names.name, covers.face_id IS NOT NULL
        FROM faces
        LEFT JOIN names ON names.face_id = faces.id
        LEFT JOIN covers ON covers.face_id = faces.id AND covers.name = names.name
        WHERE faces.photo_id = ? AND ({USABLE} OR names.face_id IS NOT NULL)
        """,
        (to_db(photo_id),)
    ).fetchall()
    return [
        {
            'id': face_id,
            'box': {'Left': left, 'Top': top, 'Width': width, 'Height': height},
            'rotation': rotation,
            'name': name,
            'cover': bool(cover)
        }
        for face_id, left, top, width, height, rotation, name, cover in rows
    ]


def face_details(face_ids):
    """
    Where each face is, for showing it in its photo
    """
    if not face_ids:
        return []
    marks = ','.join('?' * len(face_ids))
    rows = {
        row[0]: row
        for row in _db().execute(f'SELECT id, path, rotation, left, top, width, height FROM faces WHERE id IN ({marks})', face_ids)
    }
    return [
        {
            'id': face_id,
            'path': from_db(rows[face_id][1]),
            'rotation': rows[face_id][2],
            'box': dict(zip(('Left', 'Top', 'Width', 'Height'), rows[face_id][3:]))
        }
        for face_id in face_ids
        if face_id in rows
    ]


def _gallery():
    """
    The best faces of everyone with a name, to compare a group with
    """
    stamp = _stamp()[1]
    if _cache.get('gallery_stamp') != stamp:
        rows = _db().execute(
            f"""
            SELECT name, embedding FROM (
                SELECT names.name, faces.embedding, ROW_NUMBER() OVER (PARTITION BY names.name ORDER BY pixels * score DESC) AS place
                FROM names JOIN faces ON faces.id = names.face_id
                WHERE names.name <> ? AND {USABLE}
            )
            WHERE place <= ?
            """,
            (NOBODY, SUGGEST_FACES_PER_PERSON)
        ).fetchall()
        _cache.update(
            gallery_stamp=stamp,
            gallery_names=[row[0] for row in rows],
            gallery=np.array([people_db.embedding_from_db(row[1]) for row in rows], dtype=np.float32).reshape(-1, people_db.EMBEDDING_SIZE)
        )
    return _cache['gallery_names'], _cache['gallery']


def _suggested_name(cluster_faces):
    """
    Who a group's faces most look like, among faces that have a name
    """
    gallery_names, gallery = _gallery()
    if not gallery_names or not cluster_faces:
        return None
    similar = np.array([people_db.embedding_from_db(value) for value in cluster_faces]) @ gallery.T
    votes = Counter()
    for row in similar:
        for place in np.argsort(-row)[:SUGGEST_NEIGHBOURS]:
            if row[place] >= SUGGEST_SIMILARITY:
                votes[gallery_names[place]] += float(row[place])
    return votes.most_common(1)[0][0] if votes else None


def _undecided_groups(limit):
    """
    Groups of faces that haven't been decided on, largest first, with how many there are in all.
    Going through undecided faces only means naming some of a mixed group leaves the rest of it
    """
    db = _db()
    rows = db.execute(
        f"""
        SELECT cluster, COUNT(*), COUNT(DISTINCT photo_id)
        FROM faces
        WHERE cluster IS NOT NULL AND {SHOWN} AND id NOT IN (SELECT face_id FROM names)
        GROUP BY cluster
        ORDER BY COUNT(*) DESC, cluster
        """
    ).fetchall()
    several = [row for row in rows if row[1] > 1]
    return several[:limit], len(several), sum(row[1] == 1 for row in rows)


def overview(max_groups=100, preview_faces=8):
    stamp = (_stamp(), max_groups, preview_faces, _db().execute('SELECT COUNT(*), MAX(rowid) FROM covers').fetchone())
    if _cache.get('overview_stamp') == stamp:
        return _cache['overview']
    db = _db()
    faces, photos = db.execute(
        f'SELECT COUNT(*), COUNT(DISTINCT photo_id) FROM faces WHERE {USABLE} AND {SHOWN}'
    ).fetchone()
    covers = dict(db.execute(
        'SELECT covers.name, covers.face_id FROM covers JOIN names ON names.face_id = covers.face_id AND names.name = covers.name'
    ))
    people = db.execute(
        f"""
        SELECT name, COUNT(*), COUNT(DISTINCT photo_id), MAX(pixels * score), id FROM (
            SELECT names.name AS name, faces.id AS id, photo_id, pixels, score
            FROM names JOIN faces ON faces.id = names.face_id
            WHERE names.name <> ? AND {SHOWN}
        )
        GROUP BY name
        ORDER BY COUNT(*) DESC
        """,
        (NOBODY,)
    ).fetchall()
    shown, more, singles = _undecided_groups(max_groups)
    groups = []
    for cluster, count, group_photos in shown:
        best = db.execute(
            f"""
            SELECT id, embedding FROM faces
            WHERE cluster = ? AND {SHOWN} AND id NOT IN (SELECT face_id FROM names)
            ORDER BY pixels * score DESC
            LIMIT ?
            """,
            (cluster, preview_faces)
        ).fetchall()
        groups.append({
            'id': cluster,
            'faces': count,
            'photos': group_photos,
            'preview': [row[0] for row in best],
            'suggested': _suggested_name([row[1] for row in best])
        })
    result = {
        'faces': faces,
        'photos': photos,
        'people': [
            # SQLite gives best_face from the row with the largest value when a query has one MAX
            {'name': name, 'faces': count, 'photos': person_photos, 'cover': covers.get(name, best_face)}
            for name, count, person_photos, _, best_face in people
        ],
        'groups': groups,
        'more_groups': max(0, more - len(shown)),
        'single_faces': singles
    }
    _cache.update(overview_stamp=stamp, overview=result)
    return result


def group_faces(group_id, limit=PAGE_FACES):
    # best first, or nothing if there's no such group any more
    return [row[0] for row in _db().execute(
        f"""
        SELECT id FROM faces
        WHERE cluster = ? AND {SHOWN} AND id NOT IN (SELECT face_id FROM names)
        ORDER BY pixels * score DESC
        LIMIT ?
        """,
        (group_id, limit)
    )]


def group_face_ids(group_id):
    return group_faces(group_id, limit=-1)


def group_size(group_id):
    return _db().execute(
        f'SELECT COUNT(*) FROM faces WHERE cluster = ? AND {SHOWN} AND id NOT IN (SELECT face_id FROM names)',
        (group_id,)
    ).fetchone()[0]


def person_faces(name, page=1):
    """
    A page of a person's faces, best first, and how many they have in all
    """
    db = _db()
    total = db.execute(
        f"""
        SELECT COUNT(*) FROM names JOIN faces ON faces.id = names.face_id
        WHERE names.name = ? AND names.name <> ? AND {SHOWN}
        """,
        (name, NOBODY)
    ).fetchone()[0]
    rows = db.execute(
        f"""
        SELECT faces.id FROM names JOIN faces ON faces.id = names.face_id
        WHERE names.name = ? AND names.name <> ? AND {SHOWN}
        ORDER BY pixels * score DESC
        LIMIT ? OFFSET ?
        """,
        (name, NOBODY, PAGE_FACES, (page - 1) * PAGE_FACES)
    ).fetchall()
    return [row[0] for row in rows], total


def person_photo_ids(name):
    return [from_db(row[0]) for row in _db().execute(
        f"""
        SELECT DISTINCT photo_id FROM names JOIN faces ON faces.id = names.face_id
        WHERE names.name = ? AND names.name <> ? AND {SHOWN}
        """,
        (name, NOBODY)
    )]


def known_names():
    return [row[0] for row in _db().execute('SELECT DISTINCT name FROM names WHERE name <> ? ORDER BY name', (NOBODY,))]


def crop(face_id):
    """
    The path to a picture of a face, or None if there's no such face
    """
    row = _db().execute('SELECT path, rotation, left, top, width, height FROM faces WHERE id = ?', (face_id,)).fetchone()
    if row is None:
        return None
    box = dict(zip(('Left', 'Top', 'Width', 'Height'), row[2:]))
    return thumbs.face(from_db(row[0]), row[1], box, face_id)


def status():
    db = _db()
    return {
        **job.status(),
        'scanned': db.execute('SELECT COUNT(*) FROM scanned').fetchone()[0],
        'faces': db.execute(f'SELECT COUNT(*) FROM faces WHERE {USABLE}').fetchone()[0],
        'named': db.execute('SELECT COUNT(*) FROM names WHERE name <> ?', (NOBODY,)).fetchone()[0]
    }


def start(photos):
    return job.start(photos, people_db.PATH)


def has_scanned():
    return _db().execute('SELECT 1 FROM scanned LIMIT 1').fetchone() is not None


def to_scan(photos):
    """
    Those of these photos that haven't been looked at yet
    """
    scanned = people_db.scanned_photos(_db())
    return [photo for photo in photos if photo['id'] not in scanned]


def _overlap(first, second):
    left, top = max(first[0], second[0]), max(first[1], second[1])
    right, bottom = min(first[0] + first[2], second[0] + second[2]), min(first[1] + first[3], second[1] + second[3])
    shared = max(0, right - left) * max(0, bottom - top)
    return shared / (first[2] * first[3] + second[2] * second[3] - shared + 1e-9)


def _turned(box, degrees):
    # a box as (left, top, width, height) once its photo has been turned clockwise by this much
    left, top, width, height = box
    return {
        90: (1 - top - height, left, height, width),
        180: (1 - left - width, 1 - top - height, width, height),
        270: (top, 1 - left - width, height, width)
    }.get(degrees % 360, box)


def _latest_old_rows(path):
    latest = {}
    if os.path.exists(path):
        with open(path, newline='', encoding='utf-8', errors='surrogateescape') as fh:
            for row in csv.reader(fh):
                # a row cut short by a crash isn't a decision
                if len(row) == 3:
                    latest[row[0]] = row[1]
    return latest


def carry_over_old_names():
    """
    Names and covers were given to Rekognition's faces, whose ids mean nothing now.  The same face
    in the same place in the same photo is the same face, so they go across by position.  Runs once:
    the old files stay as they were, and aren't read again after this
    """
    db = _db()
    # nothing to carry them over to until faces have been found
    if db.execute("SELECT 1 FROM meta WHERE key = 'old_names_carried_over'").fetchone() or not has_scanned():
        return None
    old_names = {
        face_id: name
        for face_id, name in _latest_old_rows(OLD_NAMES_CSV).items()
        if name != OLD_UNDECIDED
    }
    old_faces = {}
    if os.path.exists(OLD_FACES):
        with open(OLD_FACES) as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for face in row['faces']:
                    box = face['box']
                    old_faces[face['face_id']] = (row['id'], row['rotation'], (box['Left'], box['Top'], box['Width'], box['Height']))
    by_photo = {}
    for face_id in old_names:
        if face_id in old_faces:
            by_photo.setdefault(old_faces[face_id][0], []).append(face_id)
    mapping = {}
    missing_photo = 0
    for photo_id, old_ids in by_photo.items():
        candidates = db.execute(
            'SELECT id, rotation, left, top, width, height FROM faces WHERE photo_id = ?',
            (to_db(photo_id),)
        ).fetchall()
        if not candidates:
            missing_photo += len(old_ids)
            continue
        pairs = []
        for old_id in old_ids:
            _, old_rotation, old_box = old_faces[old_id]
            for new_id, rotation, *box in candidates:
                # what's kept is relative to the copy each was found in, which can differ if the photo was turned between
                overlap = _overlap(_turned(old_box, rotation - old_rotation), box)
                if overlap >= OLD_MATCH_OVERLAP:
                    pairs.append((overlap, old_id, new_id))
        used = set()
        for _, old_id, new_id in sorted(pairs, reverse=True):
            if old_id not in mapping and new_id not in used:
                mapping[old_id] = new_id
                used.add(new_id)
    now = time.time_ns()
    rows = [(new_id, old_names[old_id], now) for old_id, new_id in mapping.items()]
    covers = [
        (name, mapping[face_id])
        for name, face_id in _latest_old_rows(OLD_COVERS_CSV).items()
        if face_id in mapping and old_names.get(face_id) == name
    ]
    result = {
        'names': len(old_names),
        'carried_over': len(rows),
        'photo_not_scanned': missing_photo,
        'no_matching_face': len(old_names) - len(rows) - missing_photo,
        'covers': len(covers)
    }
    with db:
        db.executemany('INSERT OR REPLACE INTO names (face_id, name, at) VALUES (?, ?, ?)', rows)
        db.executemany('INSERT INTO name_history (face_id, name, at) VALUES (?, ?, ?)', rows)
        db.executemany('INSERT OR REPLACE INTO covers (name, face_id) VALUES (?, ?)', covers)
        db.execute("INSERT INTO meta (key, value) VALUES ('old_names_carried_over', ?)", (json.dumps(result),))
    logger.info(f"Carried over old names: {result}")
    return result
