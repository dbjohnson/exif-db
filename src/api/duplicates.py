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
logger = logging.getLogger('uvicorn.error')
_resumed = False
# bits out of 64 that can differ for photos to be worth showing side by side, as
# scripts/find_duplicates.py compares them
CANDIDATE_DISTANCE = 10
CANDIDATES_SHOWN = 24
# what the duplicate check concluded about a pair, best first
VERDICT_ORDER = {copies.SAME: 0, copies.POSSIBLE: 1, None: 2, copies.DIFFERENT: 3}
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
    hide, approve, favorite, dates = [], [], [], []
    for group in groups:
        members = [photos[photo_id] for photo_id in group['photos'] if photo_id in photos]
        # the others were only found to match that one, so without it they wait for the next check
        if not group['sure'] or len(members) < 2 or group['best'] not in photos:
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
    return len(hide)


def apply_results():
    results = _read_json(RESULTS)
    if not results or results['finished'] == (_read_json(APPLIED) or {}).get('finished'):
        return
    logger.info(f"Hid {_apply(results['groups'])} duplicate photos")
    worklog.save_json(APPLIED, {'finished': results['finished']})


def _should_start():
    global _resumed
    state = job.status()['progress']
    if state is None or state['index'] != _index():
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
    if group['sure']:
        # only what this has hidden, rather than every group someone has already sorted out
        if not any(handled.get(photo['id']) == copies.HIDDEN and photo['status'] == copies.REJECTED for photo in members):
            return None
    elif len(showing) < 2 or all(photo['id'] in handled for photo in showing):
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
            'kind': 'screenshot' if copies.looks_like_screenshot(photo) else None
        }
        for photo in members
    ]
    return {
        'photo_ids': [photo['id'] for photo in members],
        'showing_ids': [photo['id'] for photo in showing],
        'best': best['id'],
        'photos': sorted(described, key=lambda photo: (not photo['best'], not photo['showing'], photo['id']))
    }


def _groups_for_page():
    groups = _groups()
    photos = {
        photo['id']: photo
        for photo in exif.duplicate_photos({photo_id for group in groups for photo_id in group['photos']})
    }
    handled = exif.handled_duplicates()
    described = ((group['sure'], _group_for_page(group, photos, handled)) for group in groups)
    sure, possible = [], []
    for is_sure, group in described:
        if group:
            (sure if is_sure else possible).append(group)
    # newest first, going by the latest photo in each
    newest = lambda group: max(photo['date'] or '' for photo in group['photos'])
    return sorted(sure, key=newest, reverse=True), sorted(possible, key=newest, reverse=True)


def page():
    sure, possible = _groups_for_page()
    return {'hidden': sure, 'possible': possible}


def status():
    sure, possible = _groups_for_page()
    return {
        **job.status(),
        'hidden': sum(not photo['showing'] for group in sure for photo in group['photos']),
        'possible': len(possible)
    }


def keep(photo_id, photo_ids):
    # keep this copy and hide the others, which takes over whatever they had
    photos = {photo['id']: photo for photo in exif.duplicate_photos(photo_ids)}
    kept = photos.get(photo_id)
    if kept is None:
        return
    others = [photo for other, photo in photos.items() if other != photo_id]
    showing = [photo for photo in others if photo['status'] != copies.REJECTED]
    approve = any(photo['status'] == copies.APPROVED for photo in showing)
    if kept['status'] == copies.REJECTED and not approve:
        exif.record_reviews(unreviewed=[photo_id])
    _record(
        hide=others,
        approve=[photo_id] if approve else [],
        favorite=[photo_id] if any(photo['favorite'] for photo in showing) else [],
        kept=[photo_id]
    )


def show(photo_ids):
    # these aren't duplicates, or are wanted anyway.  Those approved before being hidden gave that
    # to the copy that stayed, so these start again as unreviewed
    photos = exif.duplicate_photos(photo_ids)
    exif.record_duplicates([(photo['id'], copies.KEPT) for photo in photos])
    exif.record_reviews(unreviewed=[photo['id'] for photo in photos if photo['status'] == copies.REJECTED])


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


def _better(a, b, details):
    # which of two copies to keep, as the duplicate check would choose
    if a['id'] in details and b['id'] in details:
        return copies.best_copy([a, b], details)
    if _pixels(a) != _pixels(b):
        return a if _pixels(a) > _pixels(b) else b
    return max((a, b), key=copies._preference)


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
