import csv
import fcntl
import json
import os
import time
from collections import Counter

from api import thumbs


# what scripts/index_faces.py found
DIR = '/storage/rekognition'
# who each face is, as an append-only log of "<face id>,<name>,<time>"; the latest row wins
NAMES_CSV = '/storage/people.csv'
# which face stands for each person, as "<name>,<face id>,<time>"; the latest row wins, and a blank
# face goes back to choosing one automatically
COVERS_CSV = '/storage/people_covers.csv'
# a face that's been looked at and isn't anyone worth naming
NOBODY = ''
# for taking a decision back, so that the face returns to its group
UNDECIDED = '\x7fundecided'
# how alike two faces have to be, out of 100, to start out in the same group.  Higher makes more,
# smaller groups: it's easier to name several groups for a person than to untangle two in one
GROUP_SIMILARITY = 97
_cache = {}


def _indexed_path():
    return os.path.join(DIR, 'indexed.jsonl')


def _searched_path():
    return os.path.join(DIR, 'searched.jsonl')


def _stamp(*paths):
    stats = [os.stat(path) if os.path.exists(path) else None for path in paths]
    return tuple((stat.st_mtime_ns, stat.st_size) if stat else None for stat in stats)


def _jsonl(path):
    """
    Rows from a log another process is adding to, which can end in part of a row
    """
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _sharpness(face):
    return ((face.get('quality') or {}).get('Sharpness') or 0)


def _score(face):
    # how good a picture of someone this is: big and sharp
    box = face['box']
    return box['Width'] * box['Height'] * (1 + _sharpness(face))


def _load_faces():
    """
    Every face found, and which faces look like which.  Worth keeping, since it takes seconds to
    read on a NAS, and it only changes while photos are being sent
    """
    stamp = _stamp(_indexed_path(), _searched_path())
    if _cache.get('stamp') != stamp:
        faces = {}
        by_photo = {}
        for row in _jsonl(_indexed_path()):
            for face in row['faces']:
                faces[face['face_id']] = {
                    **face,
                    'photo_id': row['id'],
                    'path': row['path'],
                    'rotation': row['rotation']
                }
                by_photo.setdefault(row['id'], []).append(face['face_id'])
        # both ways, since faces sent later were only compared to from their side
        matches = {}
        for row in _jsonl(_searched_path()):
            for match in row['matches']:
                if row['face_id'] in faces and match['face_id'] in faces:
                    matches.setdefault(row['face_id'], []).append((match['face_id'], match['similarity']))
                    matches.setdefault(match['face_id'], []).append((row['face_id'], match['similarity']))
        _cache.update(stamp=stamp, faces=faces, matches=matches, by_photo=by_photo)
    return _cache['faces'], _cache['matches']


def names():
    """
    The latest name given to each face, which can be NOBODY
    """
    stamp = _stamp(NAMES_CSV)
    if _cache.get('names_stamp') != stamp:
        latest = {}
        if os.path.exists(NAMES_CSV):
            with open(NAMES_CSV, newline='', encoding='utf-8', errors='surrogateescape') as fh:
                for row in csv.reader(fh):
                    # a row cut short by a crash isn't a decision
                    if len(row) != 3:
                        continue
                    if row[1] == UNDECIDED:
                        latest.pop(row[0], None)
                    else:
                        latest[row[0]] = row[1]
        _cache.update(names_stamp=stamp, names=latest)
    return _cache['names']


def set_names(face_ids, name):
    """
    Returns what each known face was called before, for putting back with restore_names
    """
    faces, _ = _load_faces()
    current = names()
    known = [face_id for face_id in face_ids if face_id in faces]
    previous = {face_id: current.get(face_id) for face_id in known}
    _append(NAMES_CSV, [(face_id, name) for face_id in known])
    return previous


def restore_names(previous):
    # None for a face that hadn't been decided on
    _append(NAMES_CSV, [(face_id, UNDECIDED if name is None else name) for face_id, name in previous.items()])


def clear_names(face_ids):
    """
    Takes faces back to undecided, so that they return to their groups.  Returns what each was
    called before, for restore_names
    """
    faces, _ = _load_faces()
    current = names()
    previous = {face_id: current[face_id] for face_id in face_ids if face_id in faces and face_id in current}
    restore_names({face_id: None for face_id in previous})
    return previous


def rename(old, new):
    """
    Gives everyone called old the name new, which joins them up if new is already someone.  Returns
    what each face was called before, or None if there's nobody called old
    """
    members = person_faces(old)
    if not members or old == new:
        return None
    # this person's chosen cover goes along, unless the person they're joining has one
    cover = covers().get(old)
    joining_with_cover = covers().get(new) in person_faces(new)
    previous = {face_id: old for face_id in members}
    _append(NAMES_CSV, [(face_id, new) for face_id in members])
    if cover in members and not joining_with_cover:
        _append(COVERS_CSV, [(new, cover)])
    return previous


def remove(name):
    """
    Takes a person away, with their faces going back to undecided.  Returns what each face was
    called before, or None if there's nobody by that name
    """
    members = person_faces(name)
    if not members:
        return None
    return clear_names(members)


def covers():
    """
    The latest face chosen to stand for each person, whether or not it's still theirs
    """
    stamp = _stamp(COVERS_CSV)
    if _cache.get('covers_stamp') != stamp:
        latest = {}
        if os.path.exists(COVERS_CSV):
            with open(COVERS_CSV, newline='', encoding='utf-8', errors='surrogateescape') as fh:
                for row in csv.reader(fh):
                    # a row cut short by a crash isn't a decision
                    if len(row) != 3:
                        continue
                    if row[1]:
                        latest[row[0]] = row[1]
                    else:
                        latest.pop(row[0], None)
        _cache.update(covers_stamp=stamp, covers=latest)
    return _cache['covers']


def cover(name, members=None):
    """
    The face standing for a person: the one chosen if it's still theirs, or else their best
    """
    members = person_faces(name) if members is None else members
    chosen = covers().get(name)
    if chosen in members:
        return chosen
    return _best(members, 1)[0] if members else None


def chosen_cover(name):
    chosen = covers().get(name)
    return chosen if chosen in person_faces(name) else None


def set_cover(name, face_id):
    """
    Chooses the face standing for a person, or None to go back to automatic.  Returns the one chosen
    before, for putting back, or False if that face isn't this person's
    """
    if face_id is not None and names().get(face_id) != name:
        return False
    previous = chosen_cover(name)
    _append(COVERS_CSV, [(name, face_id or '')])
    return previous


def photo_faces(photo_id):
    """
    Every face found in a photo, where it is and who it's said to be.  name is None for a face that
    hasn't been decided on, and NOBODY for one that was skipped.  A box is a fraction of the copy of
    the photo that was sent, which was turned by rotation: the photo may have been turned since
    """
    faces, _ = _load_faces()
    decided = names()
    result = []
    for face_id in _cache['by_photo'].get(photo_id, ()):
        face = faces[face_id]
        name = decided.get(face_id)
        result.append({
            'id': face_id,
            'box': face['box'],
            'rotation': face['rotation'],
            'name': name,
            'cover': bool(name) and covers().get(name) == face_id
        })
    return result


def _append(path, rows):
    with open(path, 'a', newline='', encoding='utf-8', errors='surrogateescape') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        now = time.time_ns()
        csv.writer(fh, lineterminator='\n').writerows((first, second, now) for first, second in rows)


def _groups():
    """
    Faces that haven't been decided on, grouped by which look alike, largest group first.  Going
    through undecided faces only means naming some of a mixed group leaves the rest of it to name
    """
    faces, matches = _load_faces()
    decided = names()
    # working this out takes seconds on a NAS, and it only changes with the faces or their names
    stamp = (_cache['stamp'], _cache['names_stamp'])
    if _cache.get('groups_stamp') == stamp:
        return _cache['groups']
    undecided = [face_id for face_id in faces if face_id not in decided]
    leader = {face_id: face_id for face_id in undecided}

    def find(face_id):
        while leader[face_id] != face_id:
            leader[face_id] = leader[leader[face_id]]
            face_id = leader[face_id]
        return face_id

    for face_id in undecided:
        for other, similarity in matches.get(face_id, ()):
            if similarity >= GROUP_SIMILARITY and other in leader:
                first, second = find(face_id), find(other)
                # the smaller id leads, so that a group's id only changes if it's added to
                if first != second:
                    leader[max(first, second)] = min(first, second)
    members = {}
    for face_id in undecided:
        members.setdefault(find(face_id), []).append(face_id)
    groups = sorted(members.items(), key=lambda group: (-len(group[1]), group[0]))
    _cache.update(groups_stamp=stamp, groups=groups)
    return groups


def _suggested_name(group):
    """
    Who a group's faces most look like, among faces that have a name
    """
    _, matches = _load_faces()
    decided = names()
    votes = Counter()
    for face_id in group:
        for other, similarity in matches.get(face_id, ()):
            if decided.get(other):
                votes[decided[other]] += similarity
    return votes.most_common(1)[0][0] if votes else None


def _best(face_ids, limit):
    faces, _ = _load_faces()
    return sorted(face_ids, key=lambda face_id: -_score(faces[face_id]))[:limit]


def overview(max_groups=100, preview_faces=8):
    faces, _ = _load_faces()
    # a suggestion means going through every match of every face in a group
    stamp = (_cache['stamp'], _stamp(NAMES_CSV), _stamp(COVERS_CSV), max_groups, preview_faces)
    if _cache.get('overview_stamp') == stamp:
        return _cache['overview']
    by_name = {}
    for face_id, name in names().items():
        if name and face_id in faces:
            by_name.setdefault(name, []).append(face_id)
    groups = _groups()
    shown = [group for group in groups if len(group[1]) > 1][:max_groups]
    result = {
        'faces': len(faces),
        'photos': len({face['photo_id'] for face in faces.values()}),
        'people': sorted(
            (
                {
                    'name': name,
                    'faces': len(members),
                    'photos': len({faces[face_id]['photo_id'] for face_id in members}),
                    'cover': cover(name, members)
                }
                for name, members in by_name.items()
            ),
            key=lambda person: -person['faces']
        ),
        'groups': [
            {
                'id': leader,
                'faces': len(members),
                'photos': len({faces[face_id]['photo_id'] for face_id in members}),
                'preview': _best(members, preview_faces),
                'suggested': _suggested_name(members)
            }
            for leader, members in shown
        ],
        'more_groups': max(0, sum(len(group[1]) > 1 for group in groups) - len(shown)),
        'single_faces': sum(len(group[1]) == 1 for group in groups)
    }
    _cache.update(overview_stamp=stamp, overview=result)
    return result


def group_faces(group_id):
    # best first, or None if there's no such group any more
    for leader, members in _groups():
        if leader == group_id:
            return _best(members, len(members))
    return None


def person_faces(name):
    faces, _ = _load_faces()
    members = [face_id for face_id, given in names().items() if given == name and face_id in faces]
    return _best(members, len(members))


def face_details(face_ids):
    """
    Where each face is, for showing it in its photo
    """
    faces, _ = _load_faces()
    return [
        {
            'id': face_id,
            'path': faces[face_id]['path'],
            'rotation': faces[face_id]['rotation'],
            'box': faces[face_id]['box']
        }
        for face_id in face_ids
    ]


def person_photo_ids(name):
    faces, _ = _load_faces()
    return list({faces[face_id]['photo_id'] for face_id in person_faces(name)})


def known_names():
    return sorted({name for name in names().values() if name})


def crop(face_id):
    """
    The path to a picture of a face, or None if there's no such face
    """
    faces, _ = _load_faces()
    face = faces.get(face_id)
    if face is None:
        return None
    return thumbs.face(face['path'], face['rotation'], face['box'], face_id)
