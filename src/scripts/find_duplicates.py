"""
Finds photos that are copies of each other, including screenshots of a photo with an app around
it.  Started by api/duplicates.py, and safe to stop at any time: fingerprints and comparisons are
kept by file, so the next run only does what's new.  This only reports what it finds.

A fingerprint is cheap but only says two photos might match: all home screens have nearly the same
one.  So those that might are lined up and compared block by block.  How strict that is comes from
looking through a few thousand pairs of real photos: copies framed alike are nearly identical in
every block, while looser matches are bracketed exposures, annotated copies and the same screen of
a game or app showing something different.

Usage: find_duplicates.py <queue> <fingerprints> <pairs> <results> <index> <progress> <lock>
"""
import csv
import functools
import json
import math
import os
import signal
import sys
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# only these: importing the api package would also start a web app
sys.path.insert(0, os.path.join(APP_DIR, 'api'))
import worklog  # noqa: E402
from copies import DIFFERENT, POSSIBLE, RULES, SAME, best_copy, full_size_export, is_original, looks_like_screenshot, separate_shots, small_copy  # noqa: E402

# how many photos or comparisons could be redone if this is killed
SAVE_EVERY = 20
# a row of a thumbnail this even is a bar or the space between parts of an app's screen
FLAT_ROW = 5
# bits out of 64 that can differ for photos to be worth comparing
MAX_DISTANCE = 10
LOADED_PHOTOS = 100
FEATURES = 1500
MIN_MATCHES = 8
# how far one photo can be turned in another and still be the same way up
MAX_TURN_DEGREES = 1.5
GRID = 128
BLOCKS = 12
# with less variation than this, nearly anything looks the same, like two blank pages
MIN_DETAIL = 5
# how much of a photo has to be in the other for it to count as a copy of it, and how much of
# both for them to be framed alike
MIN_COVER = 0.8
# the difference allowed in the worst blocks, out of 255
SAME_FRAMING = {SAME: 5, POSSIBLE: 12}
INSIDE_ANOTHER = {SAME: 15, POSSIBLE: 30}
# a photo inside a screenshot has real matches to go on, unlike one that merely fits the frame
MIN_INSIDE_MATCHES = 30
# beyond these, one is an edited copy, like one turned black and white or a darker exposure
MAX_COLOR_CHANGE = 6
MAX_BRIGHTNESS_CHANGE = 20
MAX_CONTRAST_CHANGE = 1.25
# two screenshots the same size that are shifted are the same page, scrolled
MAX_SHIFT = 0.015
# more than this is a family of similar screens rather than copies of something
MAX_POSSIBLE_GROUP = 12
# a camera's file and an export of it with its own tone, vignette or lens correction differ block by
# block like a different photo, but line up feature for feature.  Below this many, as many pairs
# are the next shot in a burst as are copies, judging by a contact sheet of the library's 441
EXPORT_MIN_MATCHES = 400
# from here, nearly every such pair on that sheet was the same photo, so a small copy is hidden
SMALL_COPY_MIN_MATCHES = 600


def flat_run(spread):
    # the start and end of the longest run of rows that aren't flat
    best = (0, len(spread))
    longest = 0
    start = None
    for position, busy in enumerate([*(value >= FLAT_ROW for value in spread), False]):
        if busy and start is None:
            start = position
        elif not busy and start is not None:
            if position - start > longest:
                best = (start, position)
                longest = position - start
            start = None
    return best


def content_box(cv2, gray):
    """
    The part of an image that isn't bars or an app's toolbars, as left, top, right and bottom.
    Only the largest part counts, which in a screenshot of a photo is the photo
    """
    smoothed = cv2.GaussianBlur(gray, (3, 3), 0)
    top, bottom = flat_run(smoothed.std(axis=1))
    left, right = flat_run(smoothed[top:bottom].std(axis=0))
    height, width = gray.shape
    if (right - left) * (bottom - top) < 0.1 * width * height or min(right - left, bottom - top) < 16:
        return 0, 0, width, height
    return left, top, right, bottom


def perceptual_hash(cv2, np, gray):
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    low = cv2.dct(small)[:8, :8].flatten()
    return int.from_bytes(np.packbits(low > np.median(low[1:])).tobytes(), 'big')


def fingerprint(cv2, np, thumbs, path):
    # of the whole image and its content, since a copy can have bars or toolbars the other lacks
    gray = cv2.imread(thumbs.thumbnail(path), cv2.IMREAD_GRAYSCALE)
    if gray is None:
        raise ValueError(f"Can't read a thumbnail for {path}")
    left, top, right, bottom = content_box(cv2, gray)
    return f"{perceptual_hash(cv2, np, gray):016x} {perceptual_hash(cv2, np, gray[top:bottom, left:right]):016x}"


def read_log(path):
    # the latest value for each key
    if not os.path.exists(path):
        return {}
    with open(path, newline='', encoding='utf-8', errors='surrogateescape') as fh:
        # a row cut short by being killed mid-write has no time on the end
        return {row[0]: row[1] for row in csv.reader(fh) if len(row) == 3}


def candidates(np, photos, fingerprints):
    """
    Pairs of photos with similar fingerprints, closest first.  Two photos a camera took at different
    times aren't copies however alike, since they're a burst, so originals are only compared with
    those from the same moment.  Everything else is compared with everything
    """
    known = [photo for photo in photos if photo['path'] in fingerprints]
    values = [fingerprints[photo['path']].split() for photo in known]
    whole = np.array([int(value[0], 16) for value in values], dtype=np.uint64)
    content = np.array([int(value[1], 16) for value in values], dtype=np.uint64)
    originals = np.array([is_original(photo) for photo in known], dtype=bool)
    found = []
    moments = {}
    for position, photo in enumerate(known):
        if originals[position]:
            moments.setdefault(photo['taken'], []).append(position)
            continue
        distance = np.minimum.reduce([
            np.bitwise_count(values_ ^ one)
            for values_ in (whole, content)
            for one in (whole[position], content[position])
        ])
        # each pair once: by the earlier one, unless the other is an original and never gets a turn
        close = distance <= MAX_DISTANCE
        close[:position + 1] &= originals[:position + 1]
        close[position] = False
        found.extend((int(distance[other]), position, int(other)) for other in np.nonzero(close)[0])
    for same_moment in moments.values():
        for index, position in enumerate(same_moment):
            others = np.array(same_moment[index + 1:], dtype=np.intp)
            if len(others):
                distance = np.minimum.reduce([
                    np.bitwise_count(values_[others] ^ one)
                    for values_ in (whole, content)
                    for one in (whole[position], content[position])
                ])
                found.extend(
                    (int(value), position, int(other))
                    for value, other in zip(distance, others)
                    if value <= MAX_DISTANCE
                )
    found.sort()
    return [
        (known[first], known[second])
        for _, first, second in found
        if not separate_shots(known[first], known[second]) and not full_size_export(known[first], known[second])
    ]


class Comparer:
    def __init__(self, cv2, np, thumbs):
        self.cv2 = cv2
        self.np = np
        self.thumbs = thumbs
        self.orb = cv2.ORB_create(nfeatures=FEATURES, fastThreshold=10)
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        # the same photos come up again and again, as neighbors do among similar screens
        self.load = functools.lru_cache(maxsize=LOADED_PHOTOS)(self._load)

    def _load(self, path):
        image = self.cv2.imread(self.thumbs.thumbnail(path))
        if image is None:
            raise ValueError(f"Can't read a thumbnail for {path}")
        gray = self.cv2.cvtColor(image, self.cv2.COLOR_BGR2GRAY)
        points, features = self.orb.detectAndCompute(gray, None)
        locations = self.np.float32([point.pt for point in points]) if points else None
        return image, gray, locations, features, content_box(self.cv2, gray)

    def feature_alignment(self, a, b):
        # what maps b onto a according to features they share, and how many agree
        cv2, np = self.cv2, self.np
        if a[3] is None or b[3] is None or len(a[3]) < MIN_MATCHES or len(b[3]) < MIN_MATCHES:
            return None, 0
        good = [
            pair[0] for pair in self.matcher.knnMatch(b[3], a[3], k=2)
            if len(pair) == 2 and pair[0].distance < 0.8 * pair[1].distance
        ]
        if len(good) < MIN_MATCHES:
            return None, 0
        transform, inliers = cv2.estimateAffinePartial2D(
            b[2][[match.queryIdx for match in good]],
            a[2][[match.trainIdx for match in good]],
            method=cv2.RANSAC,
            ransacReprojThreshold=3,
            maxIters=2000
        )
        if transform is None:
            return None, 0
        matches = int(inliers.sum())
        turned = abs(math.degrees(math.atan2(transform[1, 0], transform[0, 0])))
        if matches < MIN_MATCHES or turned > MAX_TURN_DEGREES:
            return None, 0
        return transform, matches

    def box_alignment(self, box_a, box_b):
        # what maps one box onto the other, if they're the same shape
        left_a, top_a, right_a, bottom_a = box_a
        left_b, top_b, right_b, bottom_b = box_b
        width_a, height_a = right_a - left_a, bottom_a - top_a
        width_b, height_b = right_b - left_b, bottom_b - top_b
        if abs((width_a / width_b) / (height_a / height_b) - 1) > 0.03:
            return None
        scale = (width_a / width_b + height_a / height_b) / 2
        return self.np.array([
            [scale, 0, left_a - scale * left_b + (width_a - scale * width_b) / 2],
            [0, scale, top_a - scale * top_b + (height_a - scale * height_b) / 2]
        ])

    def sample(self, image, left, top, step, size):
        # the given area of an image as a small image, one pixel per step
        cv2 = self.cv2
        source = cv2.GaussianBlur(image, (0, 0), step / 2) if step > 1.5 else image
        place = self.np.array([[step, 0, left], [0, step, top]])
        return cv2.warpAffine(source, place, size, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)

    def difference(self, a, b, transform):
        """
        How much of each is shared and how they differ there, or None if they barely overlap.  Tone
        is evened out first, since copies get converted between color spaces and compressed
        """
        cv2, np = self.cv2, self.np
        height_a, width_a = a[1].shape
        height_b, width_b = b[1].shape
        scale = float(math.hypot(transform[0, 0], transform[1, 0]))
        corners = np.array([[0, 0], [width_b, 0], [width_b, height_b], [0, height_b]], dtype=np.float64)
        placed = corners @ transform[:, :2].T + transform[:, 2]
        left, top = max(0, placed[:, 0].min()), max(0, placed[:, 1].min())
        right, bottom = min(width_a, placed[:, 0].max()), min(height_a, placed[:, 1].max())
        if right - left < 24 or bottom - top < 24:
            return None
        overlap = (right - left) * (bottom - top)
        step = max(right - left, bottom - top) / GRID
        size = (max(8, round((right - left) / step)), max(8, round((bottom - top) / step)))
        # the same area in b
        back = cv2.invertAffineTransform(transform)
        left_b = back[0, 0] * left + back[0, 1] * top + back[0, 2]
        top_b = back[1, 0] * left + back[1, 1] * top + back[1, 2]
        sample_a = self.sample(a[0], left, top, step, size)
        sample_b = self.sample(b[0], left_b, top_b, step / scale, size)
        gray_a = cv2.GaussianBlur(cv2.cvtColor(sample_a, cv2.COLOR_BGR2GRAY).astype(np.float32), (0, 0), 0.8)
        gray_b = cv2.GaussianBlur(cv2.cvtColor(sample_b, cv2.COLOR_BGR2GRAY).astype(np.float32), (0, 0), 0.8)
        result = {
            'cover_a': overlap / (width_a * height_a),
            'cover_b': overlap / (scale * scale) / (width_b * height_b),
            'scale': scale,
            'shift': float(math.hypot(transform[0, 2] / width_a, transform[1, 2] / height_a)),
            'detail': float(min(gray_a.std(), gray_b.std()))
        }
        if result['detail'] < MIN_DETAIL:
            return {**result, 'worst': math.inf}
        evened = (gray_a - gray_a.mean()) * gray_b.std() / gray_a.std() + gray_b.mean()
        blocks = cv2.resize(np.abs(evened - gray_b), (BLOCKS, BLOCKS), interpolation=cv2.INTER_AREA)
        lab_a = cv2.cvtColor(sample_a, cv2.COLOR_BGR2LAB).astype(np.float32)
        lab_b = cv2.cvtColor(sample_b, cv2.COLOR_BGR2LAB).astype(np.float32)
        contrast = float(gray_b.std() / gray_a.std())
        return {
            **result,
            'worst': float(blocks.max()),
            'color': float(np.abs(lab_a[..., 1:] - lab_b[..., 1:]).mean()),
            'brightness': float(abs(gray_a.mean() - gray_b.mean())),
            'contrast': max(contrast, 1 / contrast)
        }

    def compare(self, photo_a, photo_b):
        """
        Whether two photos are the same, and how much more detail b has than a in what they share
        """
        a, b = self.load(photo_a['path']), self.load(photo_b['path'])
        height_a, width_a = a[1].shape
        height_b, width_b = b[1].shape
        features, matches = self.feature_alignment(a, b)
        alignments = [
            features,
            self.box_alignment((0, 0, width_a, height_a), (0, 0, width_b, height_b)),
            self.box_alignment(a[4], b[4])
        ]
        results = [
            {**result, 'matches': matches if alignment is features else 0}
            for alignment in alignments if alignment is not None
            for result in [self.difference(a, b, alignment)] if result
        ]
        if not results:
            return DIFFERENT, 1
        best = min(results, key=lambda result: result['worst'])
        verdict = judge(best, photo_a, photo_b, matches)
        # pixels in the file for each in the thumbnail, then the same area in both
        density_a = file_density(photo_a, width_a, height_a)
        density_b = file_density(photo_b, width_b, height_b)
        return verdict, density_b / best['scale'] / density_a


def file_density(photo, width, height):
    if not photo['width'] or not photo['height']:
        return 1
    return math.sqrt(photo['width'] * photo['height'] / (width * height))


def judge(result, photo_a, photo_b, matches=0):
    smaller, larger = sorted((result['cover_a'], result['cover_b']))
    if not math.isfinite(result['worst']) or larger < MIN_COVER:
        return DIFFERENT
    # a small copy of a camera's photo, such as one texted or posted, framed alike: it's sure however
    # it was edited, as it's worth nothing next to the photo itself
    small = smaller >= MIN_COVER and (small_copy(photo_a, photo_b) or small_copy(photo_b, photo_a))
    # an export next to the camera's file it came from, framed alike, however it was edited.  Only
    # possible unless it's small, as an edit is something a person decides about
    exported = is_original(photo_a) != is_original(photo_b)
    if exported and smaller >= MIN_COVER and matches >= EXPORT_MIN_MATCHES and result['worst'] > SAME_FRAMING[POSSIBLE]:
        return SAME if small and matches >= SMALL_COPY_MIN_MATCHES else POSSIBLE
    if small and result['worst'] <= SAME_FRAMING[POSSIBLE]:
        return SAME
    same_size = (photo_a['width'], photo_a['height']) == (photo_b['width'], photo_b['height'])
    if same_size and result['shift'] > MAX_SHIFT:
        return DIFFERENT
    edited = (
        result['color'] > MAX_COLOR_CHANGE
        or result['brightness'] > MAX_BRIGHTNESS_CHANGE
        or result['contrast'] > MAX_CONTRAST_CHANGE
    )
    if smaller >= MIN_COVER:
        limits = SAME_FRAMING
        sure = not edited
    else:
        limits = INSIDE_ANOTHER
        holder = photo_a if result['cover_a'] < result['cover_b'] else photo_b
        sure = not edited and looks_like_screenshot(holder) and result['matches'] >= MIN_INSIDE_MATCHES
    if sure and result['worst'] <= limits[SAME]:
        return SAME
    return POSSIBLE if result['worst'] <= limits[POSSIBLE] else DIFFERENT


class Groups:
    # sets of photos joined by links, which can be asked how much detail each has relative to another
    def __init__(self):
        self.parents = {}
        self.links = {}

    def find(self, photo):
        root = self.parents.setdefault(photo, photo)
        while self.parents[root] != root:
            root = self.parents[root]
        while self.parents[photo] != root:
            self.parents[photo], photo = root, self.parents[photo]
        return root

    def link(self, a, b, ratio):
        self.parents[self.find(a)] = self.find(b)
        self.links.setdefault(a, []).append((b, ratio))
        self.links.setdefault(b, []).append((a, 1 / ratio))

    def members(self):
        groups = {}
        for photo in list(self.parents):
            groups.setdefault(self.find(photo), []).append(photo)
        return [sorted(group) for group in groups.values() if len(group) > 1]

    def details(self, group):
        # each photo's detail, as a share of the most any in the group has
        details = {group[0]: 1.0}
        waiting = [group[0]]
        while waiting:
            photo = waiting.pop()
            for other, ratio in self.links[photo]:
                if other not in details:
                    details[other] = details[photo] * ratio
                    waiting.append(other)
        most = max(details.values())
        return {photo: detail / most for photo, detail in details.items()}

    def around_best(self, group, photos):
        """
        Splits a group into each copy with those that match it directly.  A photo resembling two
        others doesn't make those two the same, so one is only hidden for matching what's kept
        """
        details = self.details(group)
        left = set(group)
        while len(left) > 1:
            best = best_copy([photos[photo] for photo in left], details)['id']
            direct = {other: ratio for other, ratio in self.links[best] if other in left}
            left.discard(best)
            if direct:
                left -= set(direct)
                most = max(1, *direct.values())
                yield best, {best: 1 / most, **{other: ratio / most for other, ratio in direct.items()}}


RULES_FILE = 'duplicate_rules.json'
CR2 = 'CR2'


def read_rules(path):
    # which rules the pairs already compared were judged by: 1 from before there was this file
    try:
        with open(path) as fh:
            return json.load(fh)['rules']
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return 1


def pair_key(a, b):
    return f"{a['path']}\t{b['path']}"


def build_groups(comparisons, photos):
    # sure groups only join photos that are the same.  A possible one takes in what's less certain,
    # as long as that joins something new
    by_path = {photo['path']: photo for photo in photos}
    photos = {photo['id']: photo for photo in photos}
    sure = Groups()
    loose = Groups()
    for key, value in comparisons.items():
        path_a, path_b = key.split('\t', 1)
        verdict, ratio = value.split()
        a, b = by_path.get(path_a), by_path.get(path_b)
        if verdict != DIFFERENT and a and b and a['id'] != b['id'] and not separate_shots(a, b) and not full_size_export(a, b):
            loose.link(a['id'], b['id'], float(ratio))
            if verdict == SAME:
                sure.link(a['id'], b['id'], float(ratio))
    groups = []
    together = {}
    for group in sure.members():
        for best, copies_of_one in sure.around_best(group, photos):
            groups.append({'sure': True, 'best': best, 'photos': copies_of_one})
            together.update((photo, len(groups)) for photo in copies_of_one)
    for group in loose.members():
        if len({together.get(photo, photo) for photo in group}) > 1 and len(group) <= MAX_POSSIBLE_GROUP:
            groups.append({'sure': False, 'photos': loose.details(group)})
    return groups


def main(queue_path, fingerprints_path, pairs_path, results_path, index, progress_path, lock_path):
    # only one at a time
    lock = worklog.hold_lock(lock_path)
    if lock is None:
        return

    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    # only now, so that the lock shows this is running during these slow imports
    import cv2
    import numpy as np
    import thumbs

    os.nice(10)
    # leaves the NAS's other cores for serving pages
    cv2.setNumThreads(1)

    with open(queue_path) as fh:
        photos = [json.loads(line) for line in fh]
    progress = {
        'pid': os.getpid(),
        'state': 'running',
        'phase': 'fingerprints',
        'index': index,
        'started': int(time.time()),
        'finished': None,
        'total': len(photos),
        'done': 0,
        'failed': 0
    }
    worklog.save_json(progress_path, progress)

    fingerprints = read_log(fingerprints_path)
    rows = []
    for photo in photos:
        if stopping:
            break
        if photo['path'] not in fingerprints:
            try:
                value = fingerprint(cv2, np, thumbs, photo['path'])
                fingerprints[photo['path']] = value
                rows.append((photo['path'], value))
            except Exception:
                # such as a file that's gone or can't be decoded: tried again next run
                progress['failed'] += 1
        progress['done'] += 1
        if progress['done'] % SAVE_EVERY == 0:
            worklog.append_log(fingerprints_path, rows)
            rows = []
            worklog.save_json(progress_path, progress)
    worklog.append_log(fingerprints_path, rows)

    if not stopping:
        comparisons = read_log(pairs_path)
        pairs = candidates(np, photos, fingerprints)
        rules_path = os.path.join(os.path.dirname(pairs_path), RULES_FILE)
        rules = read_rules(rules_path)
        for a, b in pairs:
            key = pair_key(*sorted((a, b), key=lambda photo: photo['path']))
            if key not in comparisons:
                continue
            # a camera's photo next to an export or a small copy is looked at again, unless it was sure
            exported = is_original(a) != is_original(b) or small_copy(a, b) or small_copy(b, a)
            # before 5, older Canon CR2 files were taken for their preview's size
            raw = CR2 in (a['type'], b['type'])
            if (rules < RULES and exported and not comparisons[key].startswith(SAME)) or (rules < 5 and raw):
                del comparisons[key]
        comparer = Comparer(cv2, np, thumbs)
        progress.update(phase='comparing', started=int(time.time()), total=len(pairs), done=0)
        worklog.save_json(progress_path, progress)
        rows = []
        for a, b in pairs:
            if stopping:
                break
            a, b = sorted((a, b), key=lambda photo: photo['path'])
            key = pair_key(a, b)
            if key not in comparisons:
                try:
                    verdict, ratio = comparer.compare(a, b)
                    comparisons[key] = f"{verdict} {ratio:.4f}"
                    rows.append((key, comparisons[key]))
                except Exception:
                    progress['failed'] += 1
            progress['done'] += 1
            if progress['done'] % SAVE_EVERY == 0:
                worklog.append_log(pairs_path, rows)
                rows = []
                worklog.save_json(progress_path, progress)
        worklog.append_log(pairs_path, rows)

    if not stopping:
        worklog.save_json(rules_path, {'rules': RULES})
        worklog.save_json(results_path, {
            'finished': time.time_ns(),
            'groups': build_groups(comparisons, photos)
        })
    progress.update(state='stopped' if stopping else 'finished', finished=int(time.time()))
    worklog.save_json(progress_path, progress)


if __name__ == '__main__':
    main(*sys.argv[1:])
