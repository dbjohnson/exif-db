"""
Hides copies of photos that scripts/find_duplicates.py is sure about, and offers the ones it isn't
for a person to decide.  It runs after any scan that changes the index, so new photos are covered
without being asked, and nothing is deleted: a hidden copy can always be brought back.
"""
import json
import logging
import os

from api import copies, exif, scans, worklog
from api.jobs import Job


job = Job('duplicates', 'find_duplicates.py')
FINGERPRINTS = '/storage/duplicate_fingerprints.csv'
PAIRS = '/storage/duplicate_pairs.csv'
RESULTS = '/storage/duplicates.json'
# when the results last applied were written
APPLIED = '/storage/duplicates_applied.json'
logger = logging.getLogger('uvicorn.error')
_resumed = False


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
