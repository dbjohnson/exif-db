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
import uuid
from collections import Counter, deque

import numpy as np

from api import exif, people_db, thumbs
from api.jobs import Job
from api.people_db import from_db, to_db

logger = logging.getLogger('uvicorn.error')
job = Job('people', 'find_people.py')
dogs_job = Job('dogs', 'find_dogs.py')
# a face that's been looked at and isn't anyone worth naming
NOBODY = ''
# below these, a face isn't good enough to compare or to offer for naming: they're mostly blurs,
# the backs of heads and things that aren't faces at all, which all look alike to the model
MIN_SCORE = people_db.MIN_SCORE
MIN_PIXELS = people_db.MIN_PIXELS
USABLE = f"(score >= {MIN_SCORE} AND pixels >= {MIN_PIXELS})"
TAGGED = people_db.TAGGED
# how alike a face has to be to a named one, out of 1, to count towards being that person
SUGGEST_SIMILARITY = 0.45
# how many of a group's best faces its row shows, and get compared with named faces
GROUP_FACES = 8
# how many days a group's photos need to be from for how tight it is to mean much.  Photos taken a
# moment apart are as tight as groups get, and even 5 or more faces were mostly that, so each day
# only counts its best face.  Photos with no date all count as one day, since copies of the same
# photo at different sizes are told apart only by their size
TIGHT_DAYS = 4
# how many faces a group or a person needs to get a dot on the map
MAP_FACES = 5
# how many of each dot's most alike dots the map can draw lines to
MAP_NEIGHBOURS = 5
# how many neighbours t-SNE keeps each dot near.  On 1,008 groups, 30 kept 31% of each group's 10
# most alike close by.  300 kept only 14% but got more of the order of far-apart groups right (77% of
# clear cases against 70%); the user preferred 30, and 100 looked no better
MAP_PERPLEXITY = 30
# where the map's dots were, so that the next map starts from there instead of afresh.  Made afresh,
# adding one dot of a thousand moved the rest so much that only 44% of each dot's 10 closest stayed
# its closest; started from the last map with no exaggeration and 250 steps, 96% did, with dots
# placed as well as before.  Starts afresh when fewer than MAP_KEEP_SHARE of the dots were there
MAP_LAYOUT_PATH = os.path.join(os.path.dirname(people_db.PATH), 'people_map_layout.json')
MAP_KEEP_SHARE = 0.5
MAP_SETTLE_STEPS = 250
# how much closer than their faces alone say the map takes blood relatives to be, by how closely
# they're related: 1 for a parent, child, brother or sister, 2 for a grandparent, aunt, uncle, niece
# or nephew, 3 for a first cousin or great-grandparent.  Partners and in-laws aren't pulled, though
# two parents still end up near each other, as each is pulled to their children.  Each relative's
# dot also starts the map in the middle of theirs, as t-SNE moves a dot only a little from where it
# starts.  On 910 dots with one each and a made-up family of 14, 90% of first-degree pairs ended up
# among each other's 10 closest dots, against 24% without this, 71% of second-degree and 33% of
# third-degree against none, and the other dots kept 90% of their 10 closest
MAP_RELATIVES_CLOSER = {1: 0.3, 2: 0.6, 3: 0.85}
# that still left only 16% of children drawn with a parent among their 6 closest dots, as t-SNE packs
# a family into less room than its dots take, and pushing them apart in the browser shuffled them.
# So the browser also pulls each parent and child towards touching while it pushes dots apart, and
# siblings and two people with a child together by these much as hard, and pushes apart others in
# a family not related by blood
MAP_SIBLINGS_PULL = 0.5
MAP_PARTNERS_PULL = 1
PAGE_FACES = 500
GROUPS_PAGE = 100
# a face seems to be someone else when most of the closest of this many named faces in other photos
# aren't called what it is.  Comparing with only some of a person's faces isn't good enough: it made
# 15 times as many of the person with the most faces look wrong
DOUBT_NEIGHBOURS = 10
DOUBT_BATCH = 1000
# a face looks like someone only with at least this many of those being them, as one close face can
# be a coincidence.  Hiding a whole person's names, 5% of their faces still pass for a relative
LIKE_NEIGHBOURS = 3
# what Rekognition's results left behind, for the names given then to be carried over
OLD_FACES = '/storage/rekognition/indexed.jsonl'
OLD_NAMES_CSV = '/storage/people.csv'
OLD_COVERS_CSV = '/storage/people_covers.csv'
OLD_UNDECIDED = '\x7fundecided'
OLD_MATCH_OVERLAP = 0.3
_local = threading.local()
_cache = {}
# what's been worked out about each person's faces, as that can take the NAS a minute or two
_comparisons = {}
# one at a time, as each uses all the processor it can get
_comparisons_lock = threading.Lock()
# so that two pages loading at once don't both spend seconds on the same summary
_summary_lock = threading.Lock()
DOUBTS = 'doubts'
LIKE = 'like'
GROUPS = 'groups'
MAP = 'map'
# the ways of going through undecided groups
SUGGESTED = 'suggested'
BIGGEST = 'biggest'
TIGHTEST = 'tightest'


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


def set_names(face_ids, name, checked=False):
    """
    Returns what each known face was called before, for putting back with restore_names.  checked is
    for a face moved because it seemed to be someone else, which needn't be asked about again
    """
    previous = _current_names(face_ids)
    _record_names([(face_id, name) for face_id in previous])
    if checked:
        set_checked(list(previous), True)
    return previous


def restore_names(previous, checked=False):
    # None for a face that hadn't been decided on.  checked undoes the same in set_names
    known = _current_names(list(previous))
    _record_names([(face_id, previous[face_id]) for face_id in known])
    if checked:
        set_checked(list(known), False)


def set_not_person(face_ids, name, not_person):
    with _db() as db:
        if not_person:
            db.executemany('INSERT OR REPLACE INTO not_person (face_id, name, at) VALUES (?, ?, ?)', [(face_id, name, time.time_ns()) for face_id in face_ids])
        else:
            db.executemany('DELETE FROM not_person WHERE face_id = ? AND name = ?', [(face_id, name) for face_id in face_ids])


def set_checked(face_ids, checked):
    with _db() as db:
        if checked:
            db.executemany('INSERT OR REPLACE INTO checked_faces (face_id, at) VALUES (?, ?)', [(face_id, time.time_ns()) for face_id in face_ids])
        else:
            db.executemany('DELETE FROM checked_faces WHERE face_id = ?', [(face_id,) for face_id in face_ids])


def clear_names(face_ids):
    """
    Takes faces back to undecided, so that they return to their groups.  Returns what each was
    called before, for restore_names
    """
    previous = {face_id: name for face_id, name in _current_names(face_ids).items() if name is not None}
    _record_names([(face_id, None) for face_id in previous])
    # whatever they're called next is a new decision
    set_checked(list(previous), False)
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
    with _db() as db:
        db.execute('UPDATE OR REPLACE not_person SET name = ? WHERE name = ?', (new, old))
    if cover_face and not joining_with_cover:
        with _db() as db:
            db.execute('INSERT OR REPLACE INTO covers (name, face_id) VALUES (?, ?)', (new, cover_face))
    # being family goes along too, and so do their mom and dad, unless the person they're joining has
    # them already.  The old name keeps both, so undoing this brings them back
    if old in family():
        set_family(new, True)
    with _db() as db:
        db.execute(
            'INSERT OR IGNORE INTO parents (name, slot, parent) SELECT ?, slot, parent FROM parents WHERE name = ? AND parent <> ?',
            (new, old, new)
        )
        # their children's mom or dad becomes the new name, which undoing this doesn't change back,
        # as each child has one of each
        db.execute('UPDATE parents SET parent = ? WHERE parent = ? AND name <> ?', (new, old, new))
    return {face_id: old for face_id in members}


def family():
    """
    The names of everyone marked as family.  Removing someone leaves their mark, so that undoing it
    brings it back, so this can include names nobody has any more
    """
    return {row[0] for row in _db().execute('SELECT name FROM family')}


def set_family(name, is_family):
    with _db() as db:
        if is_family:
            db.execute('INSERT OR IGNORE INTO family (name) VALUES (?)', (name,))
        else:
            db.execute('DELETE FROM family WHERE name = ?', (name,))


def parents_of(name):
    """
    Someone's mom and dad, as {slot: name}, leaving out a slot nobody fills
    """
    return dict(_db().execute('SELECT slot, parent FROM parents WHERE name = ?', (name,)))


def set_parent(name, slot, parent):
    """
    Says who someone's mom or dad is, or with None that it's nobody named.  Returns who it was before
    """
    previous = parents_of(name).get(slot)
    with _db() as db:
        if parent is None:
            db.execute('DELETE FROM parents WHERE name = ? AND slot = ?', (name, slot))
        else:
            db.execute('INSERT OR REPLACE INTO parents (name, slot, parent) VALUES (?, ?, ?)', (name, slot, parent))
    return previous


def _family_links():
    """
    Each person's parents and children, among those who still have a name, as a name that's gone
    keeps its parents in case it comes back.  As ({child: {parents}}, {parent: {children}})
    """
    parents, children = {}, {}
    for name, parent in _db().execute(
        """
        SELECT name, parent FROM parents
        WHERE name IN (SELECT name FROM names) AND parent IN (SELECT name FROM names)
        """
    ):
        parents.setdefault(name, set()).add(parent)
        children.setdefault(parent, set()).add(name)
    return parents, children


def relatives(name):
    """
    Who someone is related to, worked out from everyone's mom and dad: their children, brothers and
    sisters, and those they have children with, as {kind: [{name, through}]}, through being the
    parent or child that relates them
    """
    parents, children = _family_links()
    siblings, partners = {}, {}
    for parent in sorted(parents.get(name, ())):
        for other in children[parent]:
            if other != name:
                siblings.setdefault(other, parent)
    for child in sorted(children.get(name, ())):
        for other in parents[child]:
            if other != name:
                partners.setdefault(other, child)
    listed = lambda found: [{'name': other, 'through': through} for other, through in sorted(found.items(), key=lambda item: item[0].lower())]
    return {
        'children': listed({child: None for child in children.get(name, ())}),
        'siblings': listed(siblings),
        'partners': listed(partners)
    }


def _relation_steps():
    """
    How closely related by blood people are, as {name: {name: degree}}, going no further than the
    degrees MAP_RELATIVES_CLOSER has.  A parent, child, brother or sister is 1, a grandparent, aunt,
    uncle, niece or nephew 2, and a first cousin or great-grandparent 3.  Those related only by
    marriage, such as two people with a child together or in-laws, aren't related here
    """
    parents, _ = _family_links()
    furthest = max(MAP_RELATIVES_CLOSER)

    def ancestors(name):
        # {ancestor: generations up}, including themselves at 0
        found = {name: 0}
        waiting = deque([name])
        while waiting:
            person = waiting.popleft()
            if found[person] == furthest:
                continue
            for parent in parents.get(person, ()):
                if parent not in found:
                    found[parent] = found[person] + 1
                    waiting.append(parent)
        return found

    everyone = set(parents) | {parent for theirs in parents.values() for parent in theirs}
    lines = {name: ancestors(name) for name in everyone}
    steps = {}
    for name, mine in lines.items():
        for other, theirs in lines.items():
            if other == name:
                continue
            # up to an ancestor they have in common and down again, which for two people both
            # descended from them counts one less: siblings are as close as a parent and child
            degree = min(
                (up + down - (1 if up and down else 0) for ancestor, up in mine.items() if (down := theirs.get(ancestor)) is not None),
                default=None
            )
            if degree is not None and degree <= furthest:
                steps.setdefault(name, {})[other] = degree
    return steps


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
        SELECT faces.id, left, top, width, height, rotation, names.name, covers.face_id IS NOT NULL, {TAGGED}
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
            'cover': bool(cover),
            # tagged by hand, with no box
            'tagged': bool(tagged)
        }
        for face_id, left, top, width, height, rotation, name, cover, tagged in rows
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


def _undecided_groups():
    """
    Groups of faces that haven't been decided on, as (group, faces, photos).  Going through undecided
    faces only means naming some of a mixed group leaves the rest of it
    """
    return _db().execute(
        f"""
        SELECT cluster, COUNT(*), COUNT(DISTINCT photo_id)
        FROM faces
        WHERE cluster IS NOT NULL AND {SHOWN} AND id NOT IN (SELECT face_id FROM names)
        GROUP BY cluster
        """
    ).fetchall()


def _place(score, faces, group):
    # highest score first, then largest first
    return (-score, -faces, group)


def _suggested(comparison, known, group):
    # matches can be from before a rename, and offering the old name would bring it back
    match = comparison['matches'].get(group)
    return match[0] if match and match[0] in known else None


def _score(view, comparison, group):
    if view == SUGGESTED:
        return comparison['matches'][group][1]
    if view == TIGHTEST:
        return comparison['tightness'][group]
    return 0


def _views(comparison):
    """
    The undecided groups in each view, in order.  Those that look like someone named are suggested,
    closest first, and the rest are either largest first or, with enough faces to say, tightest
    first.  A group is only ever suggested or one of the rest, never both
    """
    known = set(known_names())
    suggested, rest = [], []
    for row in _undecided_groups():
        (suggested if _suggested(comparison, known, row[0]) else rest).append(row)
    tight = [row for row in rest if row[0] in comparison['tightness']]

    def ordered(view, rows):
        return sorted(rows, key=lambda row: _place(_score(view, comparison, row[0]), row[1], row[0]))
    return {SUGGESTED: ordered(SUGGESTED, suggested), BIGGEST: ordered(BIGGEST, rest), TIGHTEST: ordered(TIGHTEST, tight)}


def _group_details(rows, view, comparison):
    db = _db()
    known = set(known_names())
    groups = []
    for cluster, count, group_photos in rows:
        best = db.execute(
            f"""
            SELECT id, photo_id FROM faces
            WHERE cluster = ? AND {SHOWN} AND id NOT IN (SELECT face_id FROM names)
            ORDER BY pixels * score DESC
            LIMIT ?
            """,
            # SQLite takes a limit below 0 as none
            (cluster, -1 if view == TIGHTEST else GROUP_FACES)
        ).fetchall()
        if view == TIGHTEST:
            # a group's best faces can all be from one moment, which isn't what made it tight
            days = {}
            for face, photo_id in best:
                days.setdefault(_photo_day(photo_id), face)
            preview = list(days.values())
        else:
            preview = [face for face, _ in best]
        groups.append({
            'id': cluster,
            'faces': count,
            'photos': group_photos,
            'preview': preview[:GROUP_FACES],
            'suggested': _suggested(comparison, known, cluster),
            'score': _score(view, comparison, cluster)
        })
    return groups


def _photo_day(photo_id):
    # a photo's id ends in when it was taken, or its size for one with no date
    taken = from_db(photo_id).rpartition('|')[2]
    year = taken[:4]
    return taken[:10] if year.isdigit() and int(year) >= exif.LIBRARY_EARLIEST_YEAR else None


def _best_each_day(ids):
    """
    Where the best undecided face of each day is among ids, by group, for faces that are good
    enough to compare
    """
    places = {face: place for place, face in enumerate(ids)}
    groups = {}
    for face, cluster, photo_id in _db().execute(
        f"""
        SELECT id, cluster, photo_id FROM faces
        WHERE cluster IS NOT NULL AND {SHOWN} AND id NOT IN (SELECT face_id FROM names)
        ORDER BY pixels * score DESC
        """
    ):
        if face in places:
            groups.setdefault(cluster, {}).setdefault(_photo_day(photo_id), places[face])
    return groups


def _tightness(places, embeddings):
    """
    How alike faces are, out of 1: how long their average is, as each is 1 long.  It's 1 when
    they're all the same and goes down as they spread out
    """
    return float(np.linalg.norm(embeddings[places].mean(axis=0)))


def _compare_groups(_name):
    """
    Who each undecided group looks like, as {group: (name, closeness)}, going by the closest faces
    with a name in other photos of its best faces.  Closeness is the similarity of those that are
    that person and alike enough to count, averaged over every closest face, so it's out of 1 and
    goes up as more of them agree.  Groups that nothing named is close to aren't included.  Also
    how tight each group with faces from enough days is.  Keeps the result for the page to go on using while
    the next one is worked out
    """
    ids, who, photos, embeddings = _comparable_faces()
    named = np.flatnonzero(who != '')
    matches = {}
    if len(named) > 1:
        places = {face: place for place, face in enumerate(ids)}
        best = [(group, places[face]) for group, face in _db().execute(
            f"""
            SELECT cluster, id FROM (
                SELECT cluster, id, ROW_NUMBER() OVER (PARTITION BY cluster ORDER BY pixels * score DESC) AS place
                FROM faces
                WHERE cluster IS NOT NULL AND {SHOWN} AND id NOT IN (SELECT face_id FROM names)
            )
            WHERE place <= ?
            """,
            (GROUP_FACES,)
        ) if face in places]
        compared = Counter(group for group, _ in best)
        votes = {}
        for start in range(0, len(best), DOUBT_BATCH):
            batch = best[start:start + DOUBT_BATCH]
            closest, weights = _closest_named(np.array([place for _, place in batch]), embeddings, photos, named)
            for row, column in zip(*np.nonzero(weights)):
                votes.setdefault(batch[row][0], Counter())[str(who[closest[row, column]])] += float(weights[row, column])
        for group, counted in votes.items():
            name, weight = counted.most_common(1)[0]
            matches[group] = (name, weight / (compared[group] * DOUBT_NEIGHBOURS))
    tightness = {
        group: _tightness(list(days.values()), embeddings)
        for group, days in _best_each_day(ids).items()
        if len(days) >= TIGHT_DAYS
    }
    result = {'matches': matches, 'tightness': tightness}
    _cache['group_comparison'] = result
    return result


def _group_comparison():
    """
    Who groups look like and how tight they are as of the last time that finished, or None before
    it first has, and how working out the latest is going.  Comparing with every named face can take
    the NAS a minute or two, so the page doesn't wait.  Going by only some of each person's faces
    misses most of them
    """
    entry = _comparison(GROUPS, '', _compare_groups)
    return _cache.get('group_comparison'), entry


NOTHING_COMPARED = {'matches': {}, 'tightness': {}}


def _covers():
    # the covers people have chosen, as long as the face is still theirs
    return dict(_db().execute(
        'SELECT covers.name, covers.face_id FROM covers JOIN names ON names.face_id = covers.face_id AND names.name = covers.name'
    ))


def _summary():
    """
    What every view of /people goes on: how many faces and photos there are, the people, and the
    undecided groups in each view, along with who groups look like.  That takes the NAS a few
    seconds, so it's worked out once for each state of the names and of the comparison, and also
    how working out the latest comparison is going
    """
    comparison, entry = _group_comparison()
    db = _db()
    stamp = (
        _stamp(),
        db.execute('SELECT COUNT(*), MAX(rowid) FROM covers').fetchone(),
        db.execute('SELECT GROUP_CONCAT(name, char(0)) FROM family').fetchone()
    )
    with _summary_lock:
        summary = _cache.get('summary')
        if summary and summary['stamp'] == stamp and summary['comparison'] is comparison:
            return summary, entry
        db = _db()
        faces, photos = db.execute(
            f'SELECT COUNT(*), COUNT(DISTINCT photo_id) FROM faces WHERE {USABLE} AND {SHOWN}'
        ).fetchone()
        covers = _covers()
        marked = family()
        people = db.execute(
            f"""
            SELECT name, SUM(width > 0), COUNT(DISTINCT photo_id), MAX(pixels * score), id FROM (
                SELECT names.name AS name, faces.id AS id, photo_id, pixels, score, width
                FROM names JOIN faces ON faces.id = names.face_id
                WHERE names.name <> ? AND {SHOWN}
            )
            GROUP BY name
            ORDER BY COUNT(*) DESC
            """,
            (NOBODY,)
        ).fetchall()
        compared = comparison or NOTHING_COMPARED
        summary = {
            'stamp': stamp,
            'comparison': comparison,
            'compared': compared,
            'faces': faces,
            'photos': photos,
            'people': [
                # SQLite gives best_face from the row with the largest value when a query has one MAX
                {'name': name, 'faces': count, 'photos': person_photos, 'cover': covers.get(name, best_face), 'family': name in marked}
                for name, count, person_photos, _, best_face in people
            ],
            'views': _views(compared)
        }
        _cache['summary'] = summary
    return summary, entry


def groups_after(view, score, faces, group):
    """
    The next groups in a view after one with this score and this many faces, and whether there are
    more.  Going by where the last one was, not by page, as naming a group takes it out from before
    the ones still to come
    """
    summary, _ = _summary()
    compared = summary['compared']
    after = _place(score, faces, group)
    rows = [row for row in summary['views'][view] if _place(_score(view, compared, row[0]), row[1], row[0]) > after]
    return _group_details(rows[:GROUPS_PAGE], view, compared), len(rows) > GROUPS_PAGE


def overview(view):
    """
    The people named so far, and the first of the undecided groups in a view, or none for MAP
    """
    summary, entry = _summary()
    shown = summary['views'].get(view, [])
    return {
        'faces': summary['faces'],
        'photos': summary['photos'],
        'people': summary['people'],
        'groups': _group_details(shown[:GROUPS_PAGE], view, summary['compared']),
        'more_groups': len(shown) > GROUPS_PAGE,
        'counts': {key: len(rows) for key, rows in summary['views'].items()},
        # those still comparing for the first time are the only ones with no idea who a group looks like
        'comparing': summary['comparison'] is None and entry['working'],
        'comparing_failed': entry['failed']
    }


def warm_up():
    """
    Starts working out what /people needs that takes a while, such as after a scan, so that it's
    ready when someone looks.  Says whether all of it is done
    """
    _, entry = _summary()
    map_entry = _map_entry()
    return not entry['working'] and not map_entry['working']


def _map_entry():
    # a new map when who's related changes, as well as when names do
    return _comparison(MAP, '', _work_out_map, tuple(_db().execute('SELECT * FROM parents ORDER BY 1, 2')))


def _closer(names, steps):
    """
    How much closer than their faces say each two of these names' dots are, as a square of
    multipliers: 1 for those not related, and for unnamed groups
    """
    closer = np.ones((len(names), len(names)))
    places = {}
    for place, name in enumerate(names):
        places.setdefault(name, []).append(place)
    for name, theirs in places.items():
        for other, apart in steps.get(name, {}).items():
            if other in places:
                closer[np.ix_(theirs, places[other])] = MAP_RELATIVES_CLOSER[apart]
    return closer


def _beside_relatives(spots, closer):
    """
    Each related dot moved to the middle of its relatives' dots, the closest relatives counting most
    """
    pull = 1 - closer
    related = pull.sum(axis=1) > 0
    moved = spots.copy()
    moved[related] = (pull @ spots)[related] / pull.sum(axis=1)[related, None]
    return moved


def _map_nodes(places_by_key, ids, embeddings, before, steps):
    """
    A dot for each set of faces with enough of them, as a list of {x, y, near, name, group, faces,
    tight, face}, and each one's average face, made 1 long.  x and y place those that look alike
    close together, and relatives closer still, going by steps from _relation_steps, starting from
    where they were before, given as {(name, group): (x, y)}.  near is its most alike dots, as
    [place in the list, how alike out of 1]
    """
    kept = [(key, places) for key, places in places_by_key.items() if len(places) >= MAP_FACES]
    if not kept:
        return [], np.zeros((0, people_db.EMBEDDING_SIZE), dtype=np.float32)
    averages = np.array([embeddings[places].mean(axis=0) for _, places in kept])
    tight = np.linalg.norm(averages, axis=1)
    centres = averages / tight[:, None]
    alike = centres @ centres.T
    closer = _closer([name for (name, _), _ in kept], steps)
    distances = np.clip(1 - alike.astype(np.float64), 0, None) * closer
    start = _starting_spots([key for key, _ in kept], centres, before)
    spots = _spread(centres, distances, MAP_PERPLEXITY, None if start is None else _beside_relatives(start, closer))
    np.fill_diagonal(alike, -np.inf)
    neighbours = min(MAP_NEIGHBOURS, len(kept) - 1)
    closest = np.argsort(-alike, axis=1)[:, :neighbours]
    return [
        {
            'x': round(float(x), 2),
            'y': round(float(y), 2),
            'near': [[int(other), round(float(alike[row, other]), 3)] for other in closest[row]],
            'name': name or None,
            'group': group,
            'faces': len(places),
            'tight': round(float(tightness), 3),
            'face': ids[places[0]]
        }
        for row, (((name, group), places), (x, y), tightness) in enumerate(zip(kept, spots, tight))
    ], centres


def _starting_spots(keys, centres, before):
    """
    Where each of these dots starts: where it was, or for a group that's been named since, where it
    was unnamed, or for a new one, beside the dot most like it.  None when too few were there before
    """
    by_group = {group: spot for (_, group), spot in before.items() if group is not None}
    known = {}
    for place, (name, group) in enumerate(keys):
        spot = before.get((name, group)) or (by_group.get(group) if group is not None else None)
        if spot is not None:
            known[place] = spot
    if len(keys) < 4 or len(known) < MAP_KEEP_SHARE * len(keys):
        return None
    places = np.array(list(known))
    spots = np.zeros((len(keys), 2))
    spots[places] = np.array(list(known.values()))
    new = np.array([place for place in range(len(keys)) if place not in known], dtype=int)
    if len(new):
        beside = places[(centres[new] @ centres[places].T).argmax(axis=1)]
        # a little apart, as dots in the same spot can't be told apart
        spots[new] = spots[beside] + np.random.default_rng(1).normal(0, 0.01 * spots[places].std(), (len(new), 2))
    return spots


def _spread(points, distances, perplexity, start=None):
    """
    Places points on a page, keeping each near about this many of the closest to it, going by a
    square of how far apart each two are (t-SNE).  From where they start, if given, only settling
    them, as starting afresh moves everything.  The same points always land in the same places
    """
    if len(points) < 4:
        return [(place, 0) for place in range(len(points))]
    # imported here, as they take a while and only this needs them
    from sklearn.decomposition import PCA
    from sklearn.manifold import TSNE
    np.fill_diagonal(distances, 0)
    perplexity = min(perplexity, (len(points) - 1) / 3)
    if start is None:
        # what TSNE's init='pca' does, which it can't with distances given instead of points
        start = PCA(n_components=2, svd_solver='randomized', random_state=1).fit_transform(points.astype(np.float64)).astype(np.float32)
        start = start / np.std(start[:, 0]) * 1e-4
        return TSNE(metric='precomputed', init=start, perplexity=perplexity, random_state=1).fit_transform(distances)
    return TSNE(
        metric='precomputed', init=start, perplexity=perplexity, random_state=1, early_exaggeration=1, max_iter=MAP_SETTLE_STEPS
    ).fit_transform(distances)


def _saved_layout():
    """
    Where the last map's dots were, as {'groups': {(name, group): (x, y)}, 'people': ...}, with ''
    for no name
    """
    try:
        with open(MAP_LAYOUT_PATH) as fh:
            saved = json.load(fh)
        return {mode: {(name, group): (x, y) for name, group, x, y in saved.get(mode, [])} for mode in ('groups', 'people')}
    except (FileNotFoundError, ValueError, TypeError):
        return {'groups': {}, 'people': {}}


def _save_layout(result):
    layout = {mode: [[node['name'] or '', node['group'], node['x'], node['y']] for node in nodes] for mode, nodes in result.items()}
    tmp = f"{MAP_LAYOUT_PATH}.{os.getpid()}"
    with open(tmp, 'w') as fh:
        json.dump(layout, fh)
    os.replace(tmp, MAP_LAYOUT_PATH)


def _work_out_map(_name):
    """
    Undecided groups and named people as dots placed by how alike they are, two ways: with each
    person as a dot for each of the groups their faces came from, which can tell apart what they
    looked like as a child and grown up, and with each person as one dot
    """
    ids, who, _, embeddings = _comparable_faces()
    places = {face: place for place, face in enumerate(ids)}
    by_group, by_person = {}, {}
    # best first, so each dot's first face is the one to show
    for face, cluster in _db().execute(
        f"""
        SELECT id, cluster FROM faces
        WHERE cluster IS NOT NULL AND {USABLE} AND {SHOWN}
        ORDER BY pixels * score DESC
        """
    ):
        place = places.get(face)
        if place is None:
            continue
        name = str(who[place])
        by_group.setdefault((name, cluster), []).append(place)
        by_person.setdefault((name, None) if name else (name, cluster), []).append(place)
    before = _saved_layout()
    steps = _relation_steps()
    groups, centres = _map_nodes(by_group, ids, embeddings, before['groups'], steps)
    result = {'groups': groups, 'people': _map_nodes(by_person, ids, embeddings, before['people'], steps)[0]}
    _save_layout(result)
    # together, so that what's near someone always goes with the map it was worked out on
    _cache['map'] = (result, centres)
    return result


def _drawn_family():
    parents, children = _family_links()
    links = [[child, parent, 1] for child, theirs in parents.items() for parent in sorted(theirs)]
    siblings = {tuple(sorted((first, second))) for theirs in children.values() for first in theirs for second in theirs if first != second}
    partners = {tuple(sorted((first, second))) for theirs in parents.values() for first in theirs for second in theirs if first != second}
    return (
        links
        + [[first, second, MAP_SIBLINGS_PULL] for first, second in sorted(siblings)]
        + [[first, second, MAP_PARTNERS_PULL] for first, second in sorted(partners)]
        + [link for link in _in_laws(parents) if tuple(link[:2]) not in partners]
    )


def _in_laws(parents):
    """
    Each two people in the same family who aren't related by blood, such as in-laws and partners, as
    [name, name, -1], for the map to push apart
    """
    links = {}
    for child, theirs in parents.items():
        for parent in theirs:
            links.setdefault(child, set()).add(parent)
            links.setdefault(parent, set()).add(child)
    family = {}
    for start in links:
        if start in family:
            continue
        family[start] = start
        waiting = [start]
        while waiting:
            for other in links[waiting.pop()]:
                if other not in family:
                    family[other] = start
                    waiting.append(other)
    ancestors = {}
    for name in links:
        found = {name}
        waiting = [name]
        while waiting:
            for parent in parents.get(waiting.pop(), ()):
                if parent not in found:
                    found.add(parent)
                    waiting.append(parent)
        ancestors[name] = found
    names = sorted(links)
    return [
        [first, second, -1]
        for place, first in enumerate(names) for second in names[place + 1:]
        if family[first] == family[second] and not ancestors[first] & ancestors[second]
    ]


def cluster_map(near=None):
    """
    The last map worked out, or None before the first has been, and whether a newer one is being
    worked out, which can take the NAS a minute or two.  Each person shows with their chosen cover
    when they're one dot.  Given a name, also how alike each dot with a dot for each group is to the
    closest of that person's, out of 1, in 'near'.  'family' is who to draw touching, as [name,
    name, strength]: a parent and child at 1, two siblings at MAP_SIBLINGS_PULL
    """
    entry = _map_entry()
    result, centres = _cache.get('map') or (None, None)
    found = {'working': entry['working'], 'failed': entry['failed'], 'map': result, 'family': _drawn_family()}
    if result:
        covers = _covers()
        found['map'] = {**result, 'people': [
            {**node, 'face': covers.get(node['name'], node['face'])} if node['name'] else node
            for node in result['people']
        ]}
        if near is not None:
            mine = [place for place, node in enumerate(result['groups']) if node['name'] == near]
            alike = (centres @ centres[mine].T).max(axis=1) if mine else np.zeros(len(centres))
            found['near'] = [round(float(value), 3) for value in alike]
    return found


def group_faces(group_id):
    """
    A group's undecided faces, best first, as (face, photo), or nothing if there's no such group any more
    """
    return [(face_id, from_db(photo_id)) for face_id, photo_id in _db().execute(
        f"""
        SELECT id, photo_id FROM faces
        WHERE cluster = ? AND {SHOWN} AND id NOT IN (SELECT face_id FROM names)
        ORDER BY pixels * score DESC
        """,
        (group_id,)
    )]


def person_faces(name):
    """
    A person's faces, best first, as (face, photo)
    """
    return [(face_id, from_db(photo_id)) for face_id, photo_id in _db().execute(
        f"""
        SELECT faces.id, faces.photo_id FROM names JOIN faces ON faces.id = names.face_id
        WHERE names.name = ? AND names.name <> ? AND {SHOWN} AND NOT {TAGGED}
        ORDER BY pixels * score DESC
        """,
        (name, NOBODY)
    )]


def _closest_named(part, embeddings, photos, named):
    """
    For each of the faces at places part, the closest faces in other photos among those at places
    named, and how alike each is, or 0 if they aren't alike enough to say anything
    """
    similar = embeddings[part] @ embeddings[named].T
    # the face itself is in its own photo, and nobody is in a photo twice
    similar[photos[part][:, None] == photos[named]] = -1
    neighbours = min(DOUBT_NEIGHBOURS, len(named) - 1)
    closest = np.argpartition(-similar, neighbours, axis=1)[:, :neighbours]
    weights = np.take_along_axis(similar, closest, axis=1)
    weights[weights < SUGGEST_SIMILARITY] = 0
    return named[closest], weights


def _comparable_faces():
    """
    Every face good enough to compare and not skipped, as ids, who they are ('' for undecided),
    numbers for their photos, and embeddings
    """
    rows = _db().execute(
        f"""
        SELECT faces.id, faces.photo_id, COALESCE(names.name, ''), faces.embedding
        FROM faces LEFT JOIN names ON names.face_id = faces.id
        WHERE (names.name IS NULL OR names.name <> ?) AND {USABLE} AND {SHOWN}
        """,
        (NOBODY,)
    ).fetchall()
    photo_numbers = {}
    return (
        [row[0] for row in rows],
        np.array([row[2] for row in rows]),
        np.array([photo_numbers.setdefault(row[1], len(photo_numbers)) for row in rows]),
        np.frombuffer(b''.join(row[3] for row in rows), dtype=np.float32).reshape(-1, people_db.EMBEDDING_SIZE)
    )


def _compare_with_everyone(name):
    """
    Which of a person's faces most look like someone else, going by the closest faces with a name in
    other photos, as a list of {id, row, strength}, with row being who they look like.  Faces too
    poor to compare aren't included, and those whose closest faces aren't much like them say nothing
    either way
    """
    ids, who, photos, embeddings = _comparable_faces()
    named = np.flatnonzero(who != '')
    mine = np.flatnonzero(who == name)
    found = []
    for start in range(0, len(mine), DOUBT_BATCH):
        part = mine[start:start + DOUBT_BATCH]
        closest, weights = _closest_named(part, embeddings, photos, named)
        mine_too = (weights * (who[closest] == name)).sum(axis=1)
        for row in np.flatnonzero(mine_too < weights.sum(axis=1) / 2):
            votes = Counter()
            for neighbour, weight in zip(who[closest[row]], weights[row]):
                if neighbour != name and weight:
                    votes[str(neighbour)] += float(weight)
            looks_like, weight = votes.most_common(1)[0]
            found.append({'id': ids[part[row]], 'row': looks_like, 'strength': weight})
    return found


def _faces_like(name):
    """
    The other way about: faces that aren't this person's but whose closest faces with a name mostly
    are, as a list of {id, row, strength}, with row being who they're called now, or None
    """
    ids, who, photos, embeddings = _comparable_faces()
    named = np.flatnonzero(who != '')
    mine = np.flatnonzero(who == name)
    # nobody is in a photo twice, so a photo they're already in has none of them left to find
    others = np.flatnonzero((who != name) & ~np.isin(photos, photos[mine]))
    # few faces have enough of this person's close to them, so the full comparison can go on those
    close_to_mine = np.zeros(len(others), dtype=np.int32)
    for start in range(0, len(mine), DOUBT_BATCH):
        close_to_mine += (embeddings[others] @ embeddings[mine[start:start + DOUBT_BATCH]].T >= SUGGEST_SIMILARITY).sum(axis=1)
    candidates = others[close_to_mine >= LIKE_NEIGHBOURS]
    found = []
    for start in range(0, len(candidates), DOUBT_BATCH):
        part = candidates[start:start + DOUBT_BATCH]
        closest, weights = _closest_named(part, embeddings, photos, named)
        theirs = who[closest] == name
        strength = (weights * theirs).sum(axis=1)
        alike = ((weights > 0) & theirs).sum(axis=1)
        for row in np.flatnonzero((alike >= LIKE_NEIGHBOURS) & (strength >= weights.sum(axis=1) / 2)):
            called = str(who[part[row]])
            found.append({'id': ids[part[row]], 'row': called or None, 'strength': float(strength[row])})
    return found


def _work_out_comparison(key, entry, work):
    with _comparisons_lock:
        # the names may have changed again while this waited its turn
        if _comparisons.get(key) is not entry:
            return
        try:
            entry['faces'] = work(key[1])
        except Exception:
            logger.exception(f"Can't work out {key}")
            entry['failed'] = True
        entry['working'] = False


def _comparison(kind, name, work, also=None):
    """
    What's been worked out about these faces.  Starts working it out if the names have changed, or
    also has, so 'faces' is empty while 'working' is true
    """
    stamp = (_stamp(), also)
    key = (kind, name)
    entry = _comparisons.get(key)
    if not entry or entry['stamp'] != stamp:
        entry = _comparisons[key] = {'stamp': stamp, 'working': True, 'failed': False, 'faces': []}
        threading.Thread(target=_work_out_comparison, args=(key, entry, work), daemon=True).start()
    return entry


def _biggest_rows_first(faces):
    # those without a name come first, as they're what nobody has looked at yet, then closest first
    how_many = Counter(face['row'] for face in faces)
    return sorted(faces, key=lambda face: (face['row'] is not None, -how_many[face['row']], face['row'] or '', -face['strength']))


def doubtful_faces(name):
    """
    A person's faces that seem to be someone else and nobody has settled yet, in rows by who they
    look like
    """
    entry = _comparison(DOUBTS, name, _compare_with_everyone)
    settled = {row[0] for row in _db().execute('SELECT face_id FROM checked_faces')}
    faces = [face for face in entry['faces'] if face['id'] not in settled]
    return {'working': entry['working'], 'failed': entry['failed'], 'faces': _biggest_rows_first(faces)}


def faces_like(name):
    """
    Faces not called this person that look like them and nobody has settled yet, in rows by who
    they're called now, with those without a name first
    """
    entry = _comparison(LIKE, name, _faces_like)
    db = _db()
    settled = {row[0] for row in db.execute('SELECT face_id FROM checked_faces')}
    settled.update(row[0] for row in db.execute('SELECT face_id FROM not_person WHERE name = ?', (name,)))
    faces = [face for face in entry['faces'] if face['id'] not in settled]
    return {'working': entry['working'], 'failed': entry['failed'], 'faces': _biggest_rows_first(faces)}


def comparison_working(kind, name):
    entry = _comparisons.get((kind, name))
    return bool(entry and entry['working'])


def person_photo_ids(name):
    return [from_db(row[0]) for row in _db().execute(
        f"""
        SELECT DISTINCT photo_id FROM names JOIN faces ON faces.id = names.face_id
        WHERE names.name = ? AND names.name <> ? AND {SHOWN}
        """,
        (name, NOBODY)
    )]


def tag_person(photos, name):
    """
    Says someone is in these photos without a face to go on, such as when theirs is turned away.
    photos are {'id', 'path'}, and those the person is already named in are left alone.  Returns the
    ids of the faces this adds, which clear_names takes back off
    """
    db = _db()
    already = {from_db(row[0]) for row in db.execute(
        'SELECT DISTINCT faces.photo_id FROM faces JOIN names ON names.face_id = faces.id WHERE names.name = ?',
        (name,)
    )}
    added = [(uuid.uuid4().hex, photo) for photo in photos if photo['id'] not in already]
    nothing = np.zeros(people_db.EMBEDDING_SIZE, dtype=np.float32).tobytes()
    with db:
        db.executemany(
            """
            INSERT INTO faces (id, photo_id, path, rotation, left, top, width, height, score, pixels, norm, embedding)
            VALUES (?, ?, ?, 0, 0, 0, 0, 0, ?, 0, 0, ?)
            """,
            [(face_id, to_db(photo['id']), to_db(photo['path']), people_db.TAGGED_FACE_SCORE, nothing) for face_id, photo in added]
        )
    _record_names([(face_id, name) for face_id, _ in added])
    return [face_id for face_id, _ in added]


def untag_person(photo_ids, name):
    """
    Takes someone tagged by hand off these photos, leaving their faces alone.  Returns what each was
    called before, for restore_names.  The faces stay, with no name, which nothing shows
    """
    db = _db()
    face_ids = []
    for start in range(0, len(photo_ids), 500):
        part = [to_db(photo_id) for photo_id in photo_ids[start:start + 500]]
        marks = ','.join('?' * len(part))
        face_ids += [row[0] for row in db.execute(
            f"""
            SELECT faces.id FROM faces JOIN names ON names.face_id = faces.id
            WHERE names.name = ? AND {TAGGED} AND faces.photo_id IN ({marks})
            """,
            (name, *part)
        )]
    return clear_names(face_ids)


def known_names():
    return [row[0] for row in _db().execute('SELECT DISTINCT name FROM names WHERE name <> ? ORDER BY name', (NOBODY,))]


def crop(face_id):
    """
    The path to a picture of a face, or None if there's no such face
    """
    row = _db().execute('SELECT path, rotation, left, top, width, height FROM faces WHERE id = ?', (face_id,)).fetchone()
    if row is None:
        return None
    if not row[4]:
        # tagged by hand, so the whole photo is all there is
        return thumbs.thumbnail(from_db(row[0]), row[1])
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


def has_looked_for_dogs():
    return _db().execute('SELECT 1 FROM animals_scanned LIMIT 1').fetchone() is not None


def to_look_for_dogs(photos):
    """
    Those of these photos that haven't been looked at for dogs yet
    """
    scanned = {from_db(row[0]) for row in _db().execute('SELECT photo_id FROM animals_scanned')}
    return [photo for photo in photos if photo['id'] not in scanned]


def look_for_dogs(photos):
    return dogs_job.start(photos, people_db.PATH)


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
