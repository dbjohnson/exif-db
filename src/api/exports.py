"""
Exports: the photos of chosen people copied out of the library into a folder of their own, by year
and month, with a website for looking through them that needs no server, so the folder can be put
on S3 or any other static host as it is.  scripts/export_photos.py does the copying, in the
background, and each export is kept in the storage volume until it's deleted.
"""
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import time
import zipfile

from api import exif, people, places
from api.jobs import Job
from api.worklog import save_json

EXPORTS_DIR = '/storage/exports'
job = Job('export', 'export_photos.py')
# the files an export holds: each photo's own file, or a JPEG of it as the library shows it
ORIGINAL, JPEG = 'original', 'jpeg'
# photos with any of the people chosen, or only those with all of them
ANY, ALL = 'any', 'all'
UNDATED = 'Undated'
# what can be in a file's name in an export.  S3 reads + in an address as a space, and &, # and ?
# break addresses, so anything but letters, digits, spaces and . _ - ( ) becomes _
UNSAFE = re.compile(r'[^\w .()\-]')
SLUG = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*')
# A password-protected export keeps everything but the page itself in a folder named after the
# password, worked out by PBKDF2 over it and the export's own salt, which the page works out again
# in the browser.  Without the password there's no knowing where the photos are, as S3 and other
# static hosts don't list what a folder holds.  How many rounds, which the page is told, makes
# each guess cost a browser about a tenth of a second
PASSWORD_ITERATIONS = 100_000


def folder(slug):
    return os.path.join(EXPORTS_DIR, slug)


def _spec_path(slug):
    # beside the folder, not in it, as everything in the folder gets shared
    return os.path.join(EXPORTS_DIR, f"{slug}.json")


def _load(slug):
    try:
        with open(_spec_path(slug)) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def get(slug):
    return _load(slug) if SLUG.fullmatch(slug or '') else None


def _make_slug(title):
    base = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60].strip('-') or 'export'
    # /api/exports/running stops the export being made
    if base == 'running':
        base = 'running-export'
    slug, number = base, 2
    while os.path.exists(_spec_path(slug)) or os.path.exists(folder(slug)):
        slug, number = f"{base}-{number}", number + 1
    return slug


def _refresh_hidden_faces():
    # which photos are hidden, so that a person's photos leave them out, as the library does
    stamp, shown = exif.shown_photos()
    people.set_hidden_photos(stamp, shown)


def chosen_photos(names, match):
    """
    The photos with any or all of these people, newest first and undated last, leaving out
    hidden and deleted photos as the library does
    """
    if not names:
        return []
    _refresh_hidden_faces()
    each = [set(people.person_photo_ids(name)) for name in names]
    ids = set.union(*each) if match == ANY else set.intersection(*each)
    return exif.photos_with_ids(list(ids)) if ids else []


def preview(names, match):
    """
    How many photos an export of these people would have, from which years, and how big their
    files are, as of the last scan
    """
    photos = chosen_photos(names, match)
    dates = [_when(photo)['date'] for photo in photos]
    years = sorted({date[:4] for date in dates if date})
    return {
        'photos': len(photos),
        'bytes': sum(size or 0 for size in exif.file_sizes(photo['id'] for photo in photos).values()),
        'first_year': years[0] if years else None,
        'last_year': years[-1] if years else None,
        'undated': dates.count(None)
    }


def _when(photo):
    """
    Where a photo goes in an export, by the date the library shows it under: its month's folder,
    the date as YYYY-MM-DD with the time if there's one, how to say it, and the time for the file.
    A photo with no date, or an impossible one, goes under Undated, as in the library
    """
    taken = photo['taken'] or ''
    month = re.match(r'(\d{4}):(0[1-9]|1[0-2]):', taken)
    if not month or int(month[1]) < exif.LIBRARY_EARLIEST_YEAR:
        return {'folder': UNDATED, 'date': None, 'text': None, 'mtime': None}
    try:
        at = datetime.datetime.strptime(taken[:19], '%Y:%m:%d %H:%M:%S')
    except ValueError:
        # a day that can't be, like Feb 30, still has its month
        first = datetime.date(int(month[1]), int(month[2]), 1)
        return {'folder': f"{first:%Y/%m}", 'date': f"{first:%Y-%m}", 'text': f"{first:%B %Y}", 'mtime': None}
    text = f"{at:%A}, {at:%B} {at.day}, {at.year}"
    # the time of a date given by hand doesn't mean anything
    if not photo['date_assigned']:
        text += f" · {at.hour % 12 or 12}:{at:%M %p}"
    try:
        mtime = time.mktime(at.timetuple())
    except (OverflowError, ValueError):
        mtime = None
    return {'folder': f"{at:%Y/%m}", 'date': f"{at:%Y-%m-%dT%H:%M:%S}", 'text': text, 'mtime': mtime}


def _file_name(path, files, used):
    # the photo's own name, made safe for an address, and numbered when it's taken already in that
    # month.  Without regard to case, as a folder copied onto a Mac or PC can't tell them apart
    stem, extension = os.path.splitext(os.path.basename(path))
    stem = UNSAFE.sub('_', stem).strip(' .') or 'photo'
    extension = '.jpg' if files == JPEG else UNSAFE.sub('_', extension)
    name, number = f"{stem}{extension}", 2
    while name.lower() in used:
        name, number = f"{stem} ({number}){extension}", number + 1
    used.add(name.lower())
    return name


def _items(photos, names, files):
    """
    What the worker needs for each photo: where it comes from, where it goes, and what the site
    says about it, newest first.  Names are given oldest first in each month, so that adding newer
    photos to an export later doesn't rename the files it already has
    """
    in_photos = people.names_in_photos(names)
    index = {name: number for number, name in enumerate(names)}
    used = {}
    items = {}
    for photo in sorted(photos, key=lambda photo: (photo['taken'] or '', photo['id'])):
        when = _when(photo)
        name = _file_name(photo['path'], files, used.setdefault(when['folder'], set()))
        look = f"{photo['id']}:{photo['rotation']}:{photo['enhanced']}"
        items[photo['id']] = {
            'id': photo['id'],
            'path': photo['path'],
            'rotation': photo['rotation'],
            'enhanced': photo['enhanced'],
            'taken': photo['taken'] if when['date'] else None,
            'mtime': when['mtime'],
            'file': f"photos/{when['folder']}/{name}",
            # the name of the site's own copies, which changes when the photo is turned or enhanced
            'key': hashlib.sha1(look.encode('utf-8', 'surrogateescape')).hexdigest()[:16],
            'date': when['date'],
            'when': when['text'],
            'people': sorted(index[person] for person in in_photos.get(photo['id'], ()) if person in index),
            'place': _place(photo['path'])
        }
    return [items[photo['id']] for photo in photos]


def _place(path):
    location = places.location_of(path)
    if not location:
        return None
    return location['label'] or ', '.join(part for part in (location['city'], location['state'], location['country']) if part)


def _covers(names):
    covers = {person['name']: person['cover'] for person in people.named_people()}
    return [{'name': name, 'face': covers.get(name) and people.face_box(covers[name])} for name in names]


def _secret(spec):
    # the folder a protected export's photos are in, or None for one without a password
    if not spec.get('password'):
        return None
    key = hashlib.pbkdf2_hmac('sha256', spec['password'].encode('utf-8'), bytes.fromhex(spec['salt']), spec['iterations'])
    return key.hex()[:32]


def _with_password(spec, password):
    spec['password'] = password
    if password and not spec.get('salt'):
        spec['salt'] = secrets.token_hex(16)
        spec['iterations'] = PASSWORD_ITERATIONS
    return spec


def create(title, names, match, files, password='', zipped=True):
    spec = _with_password({
        'slug': _make_slug(title),
        'title': title,
        'names': names,
        'match': match,
        'files': files,
        # a zip of the whole folder in it, for downloading everything from the site
        'zip': zipped,
        'created': int(time.time())
    }, password)
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    save_json(_spec_path(spec['slug']), spec)
    started = start(spec['slug'])
    if started is None:
        # another started first
        os.remove(_spec_path(spec['slug']))
    return started


def start(slug):
    """
    Works out which photos an export has now and starts copying them.  For an export that's been
    made before, that adds photos new to the library and takes away those that have gone from it.
    Returns the export, or None if another is being made
    """
    if job.running():
        return None
    spec = _load(slug)
    photos = chosen_photos(spec['names'], spec['match'])
    spec.update(
        folder=folder(slug),
        secret=_secret(spec),
        state='running',
        started=int(time.time()),
        people=_covers(spec['names']),
        total=len(photos)
    )
    save_json(_spec_path(slug), spec)
    if not job.start(_items(photos, spec['names'], spec['files']), _spec_path(slug)):
        return None
    return spec


def set_password(slug, password):
    """
    Gives an export a password, a new one, or with '' none, and makes it again to move its photos
    to where the new password says.  Returns the export, or None if another is being made
    """
    if job.running():
        return None
    save_json(_spec_path(slug), _with_password(_load(slug), password))
    return start(slug)


def stop():
    return job.cancel()


def listing():
    """
    Every export, newest first, with how the one being made is going
    """
    status = job.status()
    progress = status['progress']
    found = []
    if os.path.isdir(EXPORTS_DIR):
        for name in os.listdir(EXPORTS_DIR):
            if not name.endswith('.json') or name.endswith('.files.json'):
                continue
            spec = _load(name[:-len('.json')])
            if not spec:
                continue
            this_one = progress and progress.get('slug') == spec['slug']
            if spec.get('state') == 'running' and not (this_one and status['running']):
                # stopped without saying so, as in a restart
                spec['state'] = 'interrupted'
            if this_one and status['running']:
                spec['progress'] = {key: progress.get(key) for key in ('phase', 'done', 'total', 'failed', 'remaining_secs')}
            for internal in ('people', 'folder', 'secret', 'salt', 'iterations', 'zip_signature'):
                spec.pop(internal, None)
            found.append(spec)
    return sorted(found, key=lambda spec: spec['created'], reverse=True)


def delete(slug):
    """
    Deletes an export's folder and what's kept about it.  False if it's being made
    """
    status = job.status()
    if status['running'] and (status['progress'] or {}).get('slug') == slug:
        return False
    shutil.rmtree(folder(slug), ignore_errors=True)
    for path in (_spec_path(slug), os.path.join(EXPORTS_DIR, f"{slug}.files.json")):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    return True


def site_file(slug, path):
    """
    A file of an export's folder, for looking at its site from the app, or None
    """
    root = folder(slug)
    full = os.path.normpath(os.path.join(root, path or 'index.html'))
    if not full.startswith(f"{root}/") or not os.path.isfile(full):
        return None
    return full


def ready_zip(slug):
    """
    The zip an export keeps of itself, if it has everything as it was last made, or None
    """
    spec = _load(slug)
    if not spec or not spec.get('zip') or not spec.get('zip_signature') or spec.get('state') != 'finished':
        return None
    path = os.path.join(folder(slug), spec.get('secret') or '', f"{slug}.zip")
    return path if os.path.isfile(path) else None


class _Pipe:
    # where zipfile writes, handing on what it's written.  With no tell(), zipfile writes each
    # file's size after it, as it can't go back
    def __init__(self):
        self.chunks = []

    def write(self, data):
        self.chunks.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def take(self):
        data = b''.join(self.chunks)
        self.chunks.clear()
        return data


def zipped(slug):
    """
    The export's folder as a zip, made as it's sent: there's nowhere to keep a second copy of
    tens of gigabytes.  Stored, not compressed, as photos hardly compress
    """
    root = folder(slug)
    pipe = _Pipe()
    # timestamps before 1980, which zip can't hold, become 1980
    with zipfile.ZipFile(pipe, 'w', zipfile.ZIP_STORED, allowZip64=True, strict_timestamps=False) as archive:
        for directory, directories, names in os.walk(root):
            directories.sort()
            for name in sorted(names):
                # nor the zip it keeps of itself
                if name.endswith('.part') or name == f"{slug}.zip":
                    continue
                full = os.path.join(directory, name)
                info = zipfile.ZipInfo.from_file(full, os.path.join(slug, os.path.relpath(full, root)), strict_timestamps=False)
                with open(full, 'rb') as source, archive.open(info, 'w', force_zip64=True) as target:
                    while chunk := source.read(1 << 20):
                        target.write(chunk)
                        yield pipe.take()
                yield pipe.take()
    yield pipe.take()
