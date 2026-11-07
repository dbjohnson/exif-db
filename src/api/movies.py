import csv
import datetime
import os
import re

from api import exif, thumbs


MOVIES_CSV = '/storage/movies.csv'
# not home movies
EXCLUDED_FOLDERS = ('/photo/videos/Work movies',)
# the moving part of a Live Photo is a separate video of about 3 seconds
LIVE_PHOTO_MAX_SECS = 6
MIN_SECS = 1
# QuickTime files with no date set can read as 1904
EARLIEST_YEAR = 1980
# edited movies are named for when they were filmed, as in "2009.10 Lucy's Birth.mp4", and have
# the date they were exported as their own
NAME_DATE = re.compile(r'^(\d{4})\.(\d{2})(?:\.(\d{2}))?(?!\d)[\s\-_,.]*(.*)$')
LIBRARY_FOLDER_DATE = re.compile(r'^/photo/(\d{4})(?:/(\d{2}))?/')
# what browsers can read, and the codecs they can play from it
BROWSER_EXTENSIONS = ('.mov', '.mp4', '.m4v')
H264 = 'avc1'
HEVC = ('hvc1', 'hev1')
# originals above this take too long to arrive over anything but the home network
MAX_ORIGINAL_MBPS = 30
# Synology's H.264 copies of a video, best first.  Its .mov copies often have no sound, and its
# MPEG4 ones are in the old codec that browsers can't play
SYNOLOGY_COPIES = (
    'SYNOPHOTO_FILM_H264.mp4',
    'SYNOPHOTO_FILM_H.mp4',
    'SYNOPHOTO_FILM_M.mp4',
    'SYNOPHOTO_FILM_M.mov'
)
_rows_stamp = None
_rows = {}
_built_from = None
_clips = []
_copies = {}


def _clock(value):
    # "YYYY:MM:DD HH:MM:SS", which with an offset after it is already local to where it was filmed
    try:
        taken = datetime.datetime.strptime((value or '')[:19], '%Y:%m:%d %H:%M:%S')
    except ValueError:
        return None
    return taken if taken.year >= EARLIEST_YEAR else None


def _named_date(name):
    """
    The date and title in a name like "2009.10 Lucy's Birth" or "2004.05.15 - Mt Hood", as
    (date, precision, title), or None
    """
    match = NAME_DATE.match(name)
    if not match:
        return None
    year, month, day, title = match.groups()
    try:
        date = datetime.datetime(int(year), int(month), int(day or 1))
    except ValueError:
        return None
    if date.year < EARLIEST_YEAR:
        return None
    return date, 'day' if day else 'month', title.strip()


def _same_month(first, second):
    return (first.year, first.month) == (second.year, second.month)


def _when(row):
    """
    When a movie was filmed, as (date or None, 'time'/'day'/'month', title)
    """
    folder, filename = os.path.split(row['SourceFile'])
    named = _named_date(os.path.splitext(filename)[0])
    named_folder = _named_date(os.path.basename(folder))
    title = (named or named_folder or (None, None, ''))[2]
    # only phones record this one, and it's the only one known to be local time
    filmed = _clock(row.get('CreationDate'))
    recorded = _clock(row.get('DateTimeOriginal')) or _clock(row.get('CreateDate'))

    if named:
        clock = filmed or recorded
        # a day in the name is more than the clock could add, which may just be the export's
        if clock and named[1] == 'month' and _same_month(clock, named[0]):
            return clock, 'time', title
        return named[0], named[1], title
    if filmed:
        return filmed, 'time', title
    if named_folder:
        # a folder of raw clips can span the end of a month, but its copies can be much later
        if recorded and recorded.year == named_folder[0].year:
            return recorded, 'time', title
        return named_folder[0], named_folder[1], title
    if recorded:
        return recorded, 'time', title
    match = LIBRARY_FOLDER_DATE.match(row['SourceFile'])
    if match:
        return datetime.datetime(int(match[1]), int(match[2] or 1), 1), 'month', title
    return None, 'month', title


def _duration(row):
    try:
        return float(row.get('Duration') or 0)
    except ValueError:
        return 0


def _is_movie(row, duration):
    path = row['SourceFile']
    if duration < MIN_SECS or any(path.startswith(f"{folder}/") for folder in EXCLUDED_FOLDERS):
        return False
    is_live_photo = row.get('ContentIdentifier') or row.get('HasPhoto') == '1'
    return not (is_live_photo and duration < LIVE_PHOTO_MAX_SECS)


def _file_size(row):
    try:
        return int(row.get('FileSize') or 0)
    except ValueError:
        return 0


def _unique(clips):
    """
    One of each movie, the largest where there are several, with the paths of the others in its
    'copies': the same clip is often filed in more than one folder, and an edited movie can have
    been exported in two formats.  As with photos a name alone isn't enough, since cameras and
    phones reuse them
    """
    kept_by_key = {}
    unique = []
    for clip in sorted(clips, key=lambda clip: (-clip['size'], clip['path'])):
        taken = clip['sort']
        stem = os.path.splitext(os.path.basename(clip['path']))[0].lower()
        keys = {f"{stem}|{clip['month'] if _named_date(stem) else taken}"}
        if taken and clip['precision'] == 'time':
            # the same clip under another name
            keys.add(f"{taken}|{round(clip['duration'])}")
        kept = next((kept_by_key[key] for key in keys if key in kept_by_key), None)
        if kept:
            kept['copies'].append(clip['path'])
        else:
            kept = clip
            unique.append(clip)
        for key in keys:
            kept_by_key.setdefault(key, kept)
    return unique


def _natural(path):
    # so that part 2 of something comes before part 10
    return [int(part) if part.isdigit() else part for part in re.split(r'(\d+)', path.lower())]


def _build(rows, deleted):
    candidates = []
    for row in rows.values():
        duration = _duration(row)
        if row['SourceFile'] in deleted or not _is_movie(row, duration):
            continue
        taken, precision, title = _when(row)
        stem = os.path.splitext(os.path.basename(row['SourceFile']))[0].lower()
        sort = taken.isoformat() if taken else ''
        candidates.append({
            # stays the same if the file moves, as a photo's does
            'id': f"{stem}|{sort}",
            'path': row['SourceFile'],
            'taken': taken.isoformat(sep=' ') if taken else None,
            'precision': precision,
            'title': title,
            'duration': round(duration, 2),
            'copies': [],
            # only for sorting and removing duplicates
            'sort': sort,
            'month': taken.strftime('%Y-%m') if taken else '',
            'size': _file_size(row)
        })
    # undated movies go last
    ordered = sorted(
        _unique(candidates),
        key=lambda clip: (not clip['sort'], clip['sort'], _natural(clip['path']))
    )
    return [
        {key: value for key, value in clip.items() if key not in ('sort', 'month', 'size', 'copies')}
        for clip in ordered
    ], {clip['path']: clip['copies'] for clip in ordered}


def _load_rows():
    # no database in here, since playing a movie comes through a thread of its own
    global _rows_stamp, _rows
    try:
        stat = os.stat(MOVIES_CSV)
    except FileNotFoundError:
        _rows_stamp, _rows = None, {}
        return
    stamp = (stat.st_mtime_ns, stat.st_size)
    if stamp == _rows_stamp:
        return
    with open(MOVIES_CSV, newline='', encoding='utf-8', errors='surrogateescape') as fh:
        _rows = {row['SourceFile']: row for row in csv.DictReader(fh) if row.get('SourceFile')}
    _rows_stamp = stamp


def _load():
    global _built_from, _clips, _copies
    _load_rows()
    deleted = exif.deleted_paths() & _rows.keys()
    built_from = (_rows_stamp, deleted)
    if built_from != _built_from:
        _clips, _copies = _build(_rows, deleted)
        _built_from = built_from


def clips():
    """
    Every home movie in the order they were filmed, which is the order they play in, including
    hidden ones so that they can be brought back
    """
    _load()
    hidden = exif.hidden_movies()
    rotations = exif.movie_rotations()
    return [{**clip, 'hidden': clip['id'] in hidden, 'rotation': rotations.get(clip['id'], 0)} for clip in _clips]


def files_of(path):
    """
    All the copies of a movie, or None if this isn't one.  A copy left behind would take the
    movie's place in the stream as soon as it was deleted
    """
    _load()
    return [path, *_copies[path]] if path in _copies else None


def _original_plays(row, hevc):
    extension = os.path.splitext(row['SourceFile'])[1].lower()
    codec = row.get('CompressorID')
    if extension not in BROWSER_EXTENSIONS or not (codec == H264 or (hevc and codec in HEVC)):
        return False
    duration = _duration(row)
    return not duration or _file_size(row) * 8 / 1e6 / duration <= MAX_ORIGINAL_MBPS


def playable_file(path, hevc=True, copy=False):
    """
    The file to play for a movie, or None if there isn't one.  The browser says whether it can
    play HEVC, which most phone videos are, and asks for a copy if the original turned out not
    to play there.  Failing that it still gets the original to try, unless that's what failed
    """
    _load_rows()
    row = _rows.get(path)
    if row is None:
        return None
    if not copy and _original_plays(row, hevc) and os.path.isfile(path):
        return path
    for name in SYNOLOGY_COPIES:
        file = thumbs.synology_file(path, name)
        if file:
            return file
    return path if not copy and os.path.isfile(path) else None
