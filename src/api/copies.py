"""
What makes one copy of a photo better than another, and what to do about the rest.  Used by both
the web app and scripts/find_duplicates.py, so this can't import the rest of the api package.

A photo here is a dict from exif.duplicate_photos.
"""
import os
import re


SAME = 'same'
POSSIBLE = 'possible'
DIFFERENT = 'different'
# a duplicate's row in the duplicates log: hidden by this, or left showing on purpose
HIDDEN = 'hidden'
KEPT = 'kept'
# the same as in exif.py, which can't be imported from a script
APPROVED = 'approved'
REJECTED = 'rejected'
UNREVIEWED = 'unreviewed'
# how much longer than wide a phone's screen is: anything else with a photo inside it could be a
# page of them, like a scan of an album
SCREEN_SHAPE = 1.6
# copies this close in detail are as good as each other, since sizes that look different can be
# the same photo with a slightly different crop
SAME_DETAIL = 1.25
# iPhones name the copy of a photo that's been edited this way, and it's the one someone chose
EDITED_NAME = re.compile(r'IMG_E\d')
# changed whenever the duplicate check judges pairs differently, so that it looks at them again
RULES = 5
# a copy of a camera's photo with at most this share of its pixels is a small copy, like one texted
# or posted, which is hidden even edited
SMALL_COPY = 0.25
# what the check makes of a full-size export next to the camera's photo, which are both kept
EXPORT = 'export'
# a copy of a dedicated camera's photo with at least this share of its pixels, and this many, is an
# export to keep.  Facebook's copies are at most 2048 pixels across, about 2.8 million
FULL_SIZE = 0.5
FULL_SIZE_PIXELS = 4_000_000
PHONE_MAKES = {'apple', 'samsung', 'google', 'oppo', 'motorola', 'sony ericsson', 'huawei', 'xiaomi', 'oneplus', 'lg electronics'}


def is_original(photo):
    # as it came from a camera, on a date it recorded itself
    return bool(photo['camera'] and photo['taken'])




def _pixels(photo):
    return (photo['width'] or 0) * (photo['height'] or 0)


def from_dedicated_camera(photo):
    # a camera's own photo, other than a phone's, which are what get exported at full size to keep
    return is_original(photo) and (photo.get('make') or '').strip().lower() not in PHONE_MAKES


def full_size_export(a, b):
    """
    Whether one is a dedicated camera's photo, such as a DSLR's, and the other an export of it at
    (nearly) full size, such as a JPEG of a raw file.  Those are both kept, so they're never
    duplicates: only small copies are, like those texted or posted online
    """
    for original, other in ((a, b), (b, a)):
        exported = other['type'] != original['type'] or not is_original(other)
        # made from it, so no bigger, as a raw file is never an export of the camera's JPEG
        size = _pixels(other) / max(1, _pixels(original))
        big = _pixels(other) >= FULL_SIZE_PIXELS
        if from_dedicated_camera(original) and exported and big and FULL_SIZE <= size <= 1:
            return True
    return False


def small_copy(original, copy):
    # whether copy is much smaller than a camera's own photo, original
    return is_original(original) and _pixels(copy) <= SMALL_COPY * _pixels(original)


def looks_like_screenshot(photo):
    if photo['camera'] or not photo['width'] or not photo['height']:
        return False
    long_side, short_side = max(photo['width'], photo['height']), min(photo['width'], photo['height'])
    return photo['type'] == 'PNG' or long_side / short_side >= SCREEN_SHAPE


def separate_shots(a, b):
    """
    Whether two photos are different shots, however alike.  A camera's burst or timer can take
    several in a second, which are the same size but never the same bytes, unless one's an edit
    """
    if not (is_original(a) and is_original(b)):
        return False
    if a['taken'] != b['taken']:
        return True
    if EDITED_NAME.match(a['name']) or EDITED_NAME.match(b['name']):
        return False
    return sorted((a['width'], a['height'])) == sorted((b['width'], b['height'])) and a['size'] != b['size']


def _in_named_folder(photo):
    # "2002.03.11 - Escalante" says something, unlike "2000/01" or "2024 - unsorted"
    folder = os.path.basename(os.path.dirname(photo['path'])).lower()
    return any(letter.isalpha() for letter in folder) and 'unsorted' not in folder


def _preference(photo):
    return (
        photo['camera'],
        not looks_like_screenshot(photo),
        photo['dated'],
        _in_named_folder(photo),
        bool(EDITED_NAME.match(photo['name'])),
        photo['size'] or 0,
        photo['id']
    )


def best_copy(photos, details):
    """
    A camera's own file always comes first, such as a DSLR's over an export of it, as the export was
    made from it.  An iPhone's edit keeps the camera's details and is someone's choice, so it comes
    before the photo it was made from, even cropped.  Among those, or among the rest if there are
    none, the most detail, as that can't be got back, and then the most complete copy
    """
    originals = [photo for photo in photos if is_original(photo)]
    pool = [photo for photo in originals if EDITED_NAME.match(photo['name'])] or originals or photos
    sharpest = max(details[photo['id']] for photo in pool)
    return max(
        (photo for photo in pool if details[photo['id']] * SAME_DETAIL >= sharpest),
        key=_preference
    )


def plan(photos, best, handled):
    """
    What to do about a group of copies, all of which were found to match the best one: which to
    hide, and what the kept copy gets from them.  Decisions already made about a copy stand, and a
    copy that's been dealt with before isn't hidden again.  Hiding a screenshot says nothing about
    the photo in it, so a hidden copy only counts if it's better than the rest: then a worse copy
    of it isn't wanted either
    """
    extras = []
    for photo in photos:
        if photo is best or photo['id'] in handled:
            continue
        if separate_shots(photo, best):
            continue
        # someone approved a worse copy over the better one they hid
        if best['status'] == REJECTED and photo['status'] == APPROVED:
            continue
        extras.append(photo)
    showing = best['status'] != REJECTED
    showing_extras = [photo for photo in extras if photo['status'] != REJECTED]
    return {
        'best': best,
        'hide': extras,
        'approve': showing and best['status'] == UNREVIEWED and any(
            photo['status'] == APPROVED for photo in showing_extras
        ),
        'favorite': showing and not best['favorite'] and any(photo['favorite'] for photo in showing_extras),
        # one with no date of its own would otherwise go under Undated
        'date': next(
            (photo['date'][:19] for photo in showing_extras if photo['dated']),
            None
        ) if showing and not best['dated'] else None
    }
