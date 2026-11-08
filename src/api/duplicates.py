"""
Hides copies of photos that scripts/find_duplicates.py is sure about, and offers the ones it isn't
for a person to decide.  It runs after any scan that changes the index, so new photos are covered
without being asked, and nothing is deleted: a hidden copy can always be brought back.
"""
import csv
import json
import logging
import os

import numpy as np

from api import copies, exif, scans, worklog
from api.jobs import Job


job = Job('duplicates', 'find_duplicates.py')
FINGERPRINTS = exif.FINGERPRINTS_CSV
PAIRS = '/storage/duplicate_pairs.csv'
RESULTS = '/storage/duplicates.json'
# when the results last applied were written
APPLIED = '/storage/duplicates_applied.json'
# matches RULES_FILE in scripts/find_duplicates.py
RULES_PATH = '/storage/duplicate_rules.json'
logger = logging.getLogger('uvicorn.error')
_resumed = False
# bits out of 64 that can differ for photos to be worth showing side by side, as
# scripts/find_duplicates.py compares them
CANDIDATE_DISTANCE = 10
CANDIDATES_SHOWN = 24
# what the duplicate check concluded about a pair, best first
VERDICT_ORDER = {copies.SAME: 0, copies.POSSIBLE: 1, None: 2, copies.EXPORT: 3, copies.DIFFERENT: 3}
_cache = {}


def _read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _groups():
    return (_read_json(RESULTS) or {'groups': []})['groups']


def _index():
    # the same until a scan changes what's in the index
    return str(os.stat(exif.EXIF_CSV).st_mtime_ns)


def start():
    return job.start(exif.duplicate_photos(), FINGERPRINTS, PAIRS, RESULTS, _index())


def _record(hide=(), approve=(), favorite=(), dates=(), kept=()):
    # hidden as a duplicate before hidden, so that a page can't see one without the other
    exif.record_duplicates([
        *((photo['id'], copies.HIDDEN) for photo in hide),
        *((photo_id, copies.KEPT) for photo_id in kept)
    ])
    exif.record_reviews(
        approved=approve,
        rejected=[photo['id'] for photo in hide if photo['status'] != copies.REJECTED]
    )
    if favorite:
        exif.set_favorites(favorite, True)
    if dates:
        exif.restore_dates(dict(dates))


def _apply(groups):
    # hides what's sure, unless a person has already decided otherwise
    photos = {photo['id']: photo for photo in exif.duplicate_photos()}
    handled = exif.handled_duplicates()
    hide, approve, favorite, dates, swaps = [], [], [], [], []
    for group in groups:
        members = [photos[photo_id] for photo_id in group['photos'] if photo_id in photos]
        # the others were only found to match that one, so without it they wait for the next check
        if not group['sure'] or len(members) < 2 or group['best'] not in photos:
            continue
        # an earlier check hid what's now the copy to keep, such as a camera's file before those came
        # first, so that copy comes back, unless someone has decided about the group
        if handled.get(group['best']) == copies.HIDDEN and not any(
            handled.get(photo['id']) == copies.KEPT for photo in members
        ):
            swaps.append((group['best'], [photo['id'] for photo in members]))
            continue
        plan = copies.plan(members, photos[group['best']], handled)
        hide.extend(plan['hide'])
        if plan['approve']:
            approve.append(plan['best']['id'])
        if plan['favorite']:
            favorite.append(plan['best']['id'])
        if plan['date']:
            dates.append((plan['best']['id'], plan['date']))
    _record(hide, approve, favorite, dates)
    exports = _hidden_exports(photos, handled)
    if exports:
        show(exports)
        logger.info(f"Showed {len(exports)} full-size exports that an earlier check hid as copies")
    for best, photo_ids in swaps:
        keep([best], photo_ids)
    if swaps:
        logger.info(f"Kept {len(swaps)} copies that an earlier check hid, in place of the copy it kept")
    return len(hide)


def _hidden_exports(photos, handled):
    # full-size exports an earlier check hid as copies of a camera's photo, unless someone kept that
    by_path = {photo['path']: photo for photo in photos.values()}
    found = set()
    for (path_a, path_b), verdict in _comparisons().items():
        a, b = by_path.get(path_a), by_path.get(path_b)
        if verdict == copies.DIFFERENT or not a or not b or not copies.full_size_export(a, b):
            continue
        for hidden, other in ((a, b), (b, a)):
            if handled.get(hidden['id']) == copies.HIDDEN and handled.get(other['id']) != copies.KEPT:
                found.add(hidden['id'])
    return sorted(found)


def apply_results():
    results = _read_json(RESULTS)
    if not results or results['finished'] == (_read_json(APPLIED) or {}).get('finished'):
        return
    logger.info(f"Hid {_apply(results['groups'])} duplicate photos")
    worklog.save_json(APPLIED, {'finished': results['finished']})


def _rules():
    # which rules what's been compared was judged by
    return (_read_json(RULES_PATH) or {}).get('rules', 1)


def _should_start():
    global _resumed
    state = job.status()['progress']
    if state is None or state['index'] != _index():
        return True
    # pairs judged by older rules are looked at again, once, unless someone stopped that
    if state['state'] == 'finished' and _rules() != copies.RULES:
        return True
    # a run cut off by a restart carries on, but not one that keeps falling over by itself
    if state['state'] == 'interrupted' and not _resumed:
        _resumed = True
        return True
    return False


def check():
    # called now and then on the event loop, which is the only place the index's connection is used
    if job.running():
        return
    apply_results()
    if not scans.running() and _should_start():
        start()


def _group_for_page(group, photos, handled):
    members = [photos[photo_id] for photo_id in group['photos'] if photo_id in photos]
    if len(members) < 2:
        return None
    showing = [photo for photo in members if photo['status'] != copies.REJECTED]
    waiting = not group['sure'] and len(showing) >= 2 and not all(photo['id'] in handled for photo in showing)
    # otherwise only for what's hidden as a copy, rather than every group someone has said aren't copies
    if not waiting and not any(
        handled.get(photo['id']) == copies.HIDDEN and photo['status'] == copies.REJECTED for photo in members
    ):
        return None
    best = photos.get(group.get('best'))
    if best is None or best['status'] == copies.REJECTED:
        best = copies.best_copy(showing or members, group['photos'])
    described = [
        {
            **photo,
            'showing': photo['status'] != copies.REJECTED,
            'best': photo is best,
            'folder': os.path.relpath(os.path.dirname(photo['path']), exif.LIBRARY_ROOT),
            'screenshot': copies.looks_like_screenshot(photo),
            'hidden_as_copy': photo['status'] == copies.REJECTED and handled.get(photo['id']) == copies.HIDDEN
        }
        for photo in members
    ]
    return {
        'waiting': waiting,
        'photo_ids': [photo['id'] for photo in members],
        'showing_ids': [photo['id'] for photo in showing],
        'best': best['id'],
        'photos': sorted(described, key=lambda photo: (not photo['best'], not photo['showing'], photo['id']))
    }


def _with_cameras(groups):
    # what each photo was taken with, which is worth comparing but slow to find for every group at once
    cameras = exif.cameras_of([photo['id'] for group in groups for photo in group['photos']])
    for group in groups:
        for photo in group['photos']:
            photo['camera_name'] = cameras.get(photo['id'])
    return groups


def _groups_for_page():
    groups = _groups()
    photos = {
        photo['id']: photo
        for photo in exif.duplicate_photos({photo_id for group in groups for photo_id in group['photos']})
    }
    handled = exif.handled_duplicates()
    sure, possible = [], []
    for group in groups:
        described = _group_for_page(group, photos, handled)
        if described:
            (possible if described['waiting'] else sure).append(described)
    # newest first, going by the latest photo in each
    newest = lambda group: max(photo['date'] or '' for photo in group['photos'])
    return sorted(sure, key=newest, reverse=True), sorted(possible, key=newest, reverse=True)


def page():
    sure, possible = _groups_for_page()
    return {
        'possible': len(possible),
        'hidden_groups': len(sure),
        'hidden': sum(not photo['showing'] for group in sure for photo in group['photos'])
    }


def to_review():
    # every group waiting for a decision, to go through one at a time
    return _with_cameras(_groups_for_page()[1])


def hidden_groups(offset, limit):
    # a page of the groups with copies hidden, automatically or by a person, which are too many to show at once
    sure = _groups_for_page()[0]
    return {'total': len(sure), 'groups': _with_cameras(sure[offset:offset + limit])}


def status():
    sure, possible = _groups_for_page()
    return {
        **job.status(),
        'hidden': sum(not photo['showing'] for group in sure for photo in group['photos']),
        'possible': len(possible)
    }


def _group_of(photo_ids):
    # how the group these photos are in stands now, or None if there's nothing left to show of it
    wanted = set(photo_ids)
    group = next((group for group in _groups() if wanted & set(group['photos'])), None)
    if group is None:
        return None
    photos = {photo['id']: photo for photo in exif.duplicate_photos(group['photos'])}
    described = _group_for_page(group, photos, exif.handled_duplicates())
    return described and _with_cameras([described])[0]


def _before(photo_ids):
    # what a decision about these photos changes, as restore takes it back
    handled = exif.handled_duplicates()
    return [
        {'id': photo['id'], 'status': photo['status'], 'favorite': photo['favorite'], 'handled': handled.get(photo['id'], '')}
        for photo in exif.duplicate_photos(photo_ids)
    ]


def keep(kept_ids, photo_ids):
    """
    Keep these copies and hide the others, of which the first kept takes over whatever the hidden
    ones had.  Returns what was there before, for restore, and how the group stands now
    """
    previous = _before(photo_ids)
    photos = {photo['id']: photo for photo in exif.duplicate_photos(photo_ids)}
    kept = [photos[photo_id] for photo_id in dict.fromkeys(kept_ids) if photo_id in photos]
    if not kept:
        return {'previous': [], 'group': _group_of(photo_ids)}
    kept_set = {photo['id'] for photo in kept}
    others = [photo for photo_id, photo in photos.items() if photo_id not in kept_set]
    showing = [photo for photo in others if photo['status'] != copies.REJECTED]
    approve = any(photo['status'] == copies.APPROVED for photo in showing)
    first = kept[0]['id']
    # kept on purpose, so not left hidden.  Those approved before being hidden gave that to the copy
    # that stayed, so these start again as unreviewed
    exif.record_reviews(unreviewed=[
        photo['id'] for photo in kept
        if photo['status'] == copies.REJECTED and not (approve and photo['id'] == first)
    ])
    _record(
        hide=others,
        approve=[first] if approve else [],
        favorite=[first] if any(photo['favorite'] for photo in showing) and not kept[0]['favorite'] else [],
        kept=kept_set
    )
    return {'previous': previous, 'group': _group_of(photo_ids)}


def show(photo_ids):
    # these aren't duplicates, or are wanted anyway
    return keep(photo_ids, photo_ids)


def restore(previous):
    """
    Undoes keep or show, given what they returned: each photo's review, favorite and duplicate
    decision as they were.  Returns how the group stands again
    """
    if not previous:
        return None
    exif.record_duplicates([(photo['id'], photo['handled']) for photo in previous])
    exif.restore_reviews({photo['id']: photo['status'] for photo in previous})
    exif.set_favorites([photo['id'] for photo in previous if photo['favorite']], True)
    exif.set_favorites([photo['id'] for photo in previous if not photo['favorite']], False)
    return _group_of([photo['id'] for photo in previous])


# ---- possible duplicates of one photo, to compare side by side

def _log(path):
    # an append-only "<key>,<value>,<time>" log, of which the latest row for each key counts
    found = {}
    if os.path.exists(path):
        with open(path, newline='', encoding='utf-8', errors='surrogateescape') as fh:
            for row in csv.reader(fh):
                if len(row) >= 2:
                    found[row[0]] = row[1]
    return found


def _fingerprints():
    # every photo's two hashes, as numbers, and where each path's are
    stamp = exif._file_stamps(FINGERPRINTS)
    if _cache.get('fingerprints_stamp') != stamp:
        paths, whole, content = [], [], []
        for path, value in _log(FINGERPRINTS).items():
            try:
                first, second = (int(part, 16) for part in value.split())
            except ValueError:
                continue
            paths.append(path)
            whole.append(first)
            content.append(second)
        _cache.update(
            fingerprints_stamp=stamp,
            fingerprints=(paths, {path: place for place, path in enumerate(paths)}, np.array(whole, dtype=np.uint64), np.array(content, dtype=np.uint64))
        )
    return _cache['fingerprints']


def _comparisons():
    # what the check concluded about each pair it compared, by their paths in order
    stamp = exif._file_stamps(PAIRS)
    if _cache.get('pairs_stamp') != stamp:
        found = {}
        for key, value in _log(PAIRS).items():
            parts = value.split()
            if '\t' in key and len(parts) == 2:
                found[tuple(sorted(key.split('\t', 1)))] = parts[0]
        _cache.update(pairs_stamp=stamp, pairs=found)
    return _cache['pairs']


def _near(path):
    # the paths whose fingerprints are close to this one's, with how many bits apart
    paths, where, whole, content = _fingerprints()
    place = where.get(path)
    if place is None:
        return None
    distance = np.minimum.reduce([
        np.bitwise_count(values ^ one)
        for values in (whole, content)
        for one in (whole[place], content[place])
    ])
    close = np.nonzero(distance <= CANDIDATE_DISTANCE)[0]
    return {paths[other]: int(distance[other]) for other in close if other != place}


def _pixels(photo):
    return (photo['width'] or 0) * (photo['height'] or 0)


def _best_of(photos, details):
    # which copy to keep, as the duplicate check would choose, going by pixels for any it didn't group
    if not all(photo['id'] in details for photo in photos):
        most = max(_pixels(photo) for photo in photos) or 1
        details = {photo['id']: (_pixels(photo) / most) ** 0.5 for photo in photos}
    return copies.best_copy(photos, details)


def _better(a, b, details):
    return _best_of([a, b], details)


def _described(photo, cameras):
    return {
        **photo,
        'folder': os.path.relpath(os.path.dirname(photo['path']), exif.LIBRARY_ROOT),
        'camera_name': cameras.get(photo['id']),
        'screenshot': copies.looks_like_screenshot(photo),
        'hidden_as_copy': photo['status'] == copies.REJECTED and exif.handled_duplicates().get(photo['id']) == copies.HIDDEN
    }


def candidates(photo_id):
    """
    Photos that may be copies of this one, for comparing it with each side by side: those whose
    fingerprints are close, and any the check grouped with it.  Each says what the check made of
    the pair, if it compared them, and which of the two looks like the copy to keep.  None for a
    photo that isn't in the index
    """
    found = exif.duplicate_photos([photo_id])
    if not found:
        return None
    photo = found[0]
    near = _near(photo['path'])
    group = next((group for group in _groups() if photo_id in group['photos']), None)
    others = [
        other for other in exif.duplicate_photos(group['photos'] if group else (), near or {})
        if other['id'] != photo_id
    ]
    details = group['photos'] if group and isinstance(group['photos'], dict) else {}
    pairs = _comparisons()
    cameras = exif.cameras_of([photo_id, *(other['id'] for other in others)])
    shown = []
    for other in others:
        verdict = pairs.get(tuple(sorted((photo['path'], other['path']))))
        if copies.full_size_export(photo, other):
            verdict = copies.EXPORT
        better = _better(photo, other, details)
        shown.append({
            **_described(other, cameras),
            'verdict': verdict,
            'distance': (near or {}).get(other['path']),
            'in_group': bool(group and other['id'] in group['photos']),
            'better': better is other
        })
    shown.sort(key=lambda other: (
        VERDICT_ORDER.get(other['verdict'], 2),
        not other['in_group'],
        other['distance'] if other['distance'] is not None else CANDIDATE_DISTANCE + 1
    ))
    return {
        'photo': _described(photo, cameras),
        # the photo's other files, such as the same file backed up in another folder, which take its
        # place if only this one is deleted
        'other_files': [path for path in exif.copies_of(photo['path']) if path != photo['path']],
        'checked': near is not None,
        'candidates': shown[:CANDIDATES_SHOWN]
    }


# the most that can be decided about at once, one for each number key
SHOWN_WITH_PHOTO = 9


def for_photo(photo_id, everything=False):
    """
    A photo and what may be copies of it as a group to decide about, as on the duplicates page:
    those the check didn't find different, or everything that looks like it.  Also how many more
    there would be with everything.  None for a photo that isn't in the index
    """
    found = candidates(photo_id)
    if found is None:
        return None
    likely = [
        other for other in found['candidates']
        if everything or other['verdict'] not in (copies.DIFFERENT, copies.EXPORT)
    ]
    members = [{**found['photo'], 'this': True}, *likely][:SHOWN_WITH_PHOTO]
    result = {
        'photo': found['photo'],
        'checked': found['checked'],
        'more': len(found['candidates']) - len(likely),
        'group': None
    }
    if len(members) < 2:
        return result
    group = next((group for group in _groups() if photo_id in group['photos']), None)
    details = group['photos'] if group and isinstance(group['photos'], dict) else {}
    best = _best_of(members, details)
    described = [
        {**photo, 'showing': photo['status'] != copies.REJECTED, 'best': photo['id'] == best['id']}
        for photo in members
    ]
    result['group'] = {
        'waiting': False,
        'photo_ids': [photo['id'] for photo in described],
        'showing_ids': [photo['id'] for photo in described if photo['showing']],
        'best': best['id'],
        'photos': sorted(described, key=lambda photo: (not photo['best'], not photo.get('this'), not photo['showing']))
    }
    return result
