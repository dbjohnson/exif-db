"""
Copies an export's photos into its folder, by year and month, and writes the website that shows
them.  Started by api/exports.py, and safe to stop at any time: running it again skips the files
already there and unchanged, so it also brings an export up to date.

Usage: export_photos.py <queue> <spec> <progress> <lock>
"""
import hashlib
import html
import json
import os
import re
import shutil
import signal
import sys
import time
import zipfile

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# what the site is made from, copied into every export, with the app's own icon
SITE_DIR = os.path.join(APP_DIR, 'export_site')
STATIC_DIR = os.path.join(APP_DIR, 'static')
SITE_FILES = (
    (SITE_DIR, 'export.css'),
    (SITE_DIR, 'export.js'),
    (SITE_DIR, 'unlock.js'),
    (STATIC_DIR, 'favicon-32x32.png'),
    (STATIC_DIR, 'apple-touch-icon.png')
)
# only these: importing the api package would also start a web app
sys.path.insert(0, os.path.join(APP_DIR, 'api'))
import worklog  # noqa: E402

ORIGINAL, JPEG = 'original', 'jpeg'
# what a password moves into a folder of its own: everything but the page and its scripts
CONTENT = ('photos', 'site/thumbs', 'site/large', 'site/people', 'site/data.js')
# the name of that folder, which the password works out
SECRET_FOLDER = re.compile(r'[0-9a-f]{32}')
JPEG_QUALITY = 92
# how many photos could be copied again if this is killed
SAVE_EVERY = 20
# the EXIF tags written into a JPEG copy: its orientation, as the copy is already upright, and
# when it was taken, which may have been set by hand
ORIENTATION = 0x0112
EXIF_IFD = 0x8769
DATE_TIME_ORIGINAL = 0x9003


def _copy(source, target):
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(source, f"{target}.part")
    os.replace(f"{target}.part", target)


def _same(path, data):
    try:
        with open(path, 'rb') as fh:
            return fh.read() == data
    except FileNotFoundError:
        return False


def _copy_small(source, target):
    # a file of the site's own, left alone when it hasn't changed, so the zip needn't be made again
    with open(source, 'rb') as fh:
        if not _same(target, fh.read()):
            _copy(source, target)


def _exif(Image, photo):
    """
    The photo's own EXIF for its JPEG copy, so that its camera and location go along, with the
    orientation and date put right.  Only the date, if the photo's own can't be read
    """
    path = photo['path']
    try:
        if path.split('.')[-1].upper() == 'HEIC':
            import pyheif
            exif = Image.Exif()
            for block in pyheif.open(path).metadata or []:
                if block['type'] == 'Exif':
                    data = block['data']
                    exif.load(data[data.find(b'Exif'):])
        else:
            with Image.open(path) as im:
                exif = im.getexif()
        exif[ORIENTATION] = 1
        if photo['taken']:
            exif.get_ifd(EXIF_IFD)[DATE_TIME_ORIGINAL] = photo['taken'][:19]
        return exif.tobytes()
    except Exception:
        exif = Image.Exif()
        if photo['taken']:
            exif.get_ifd(EXIF_IFD)[DATE_TIME_ORIGINAL] = photo['taken'][:19]
        return exif.tobytes()


def _jpeg(thumbs, Image, photo, target):
    try:
        im = thumbs.full_size(photo['path'], photo['rotation'], photo['enhanced'])
    except Exception:
        # such as a raw file this can't develop: the copy for the screen is better than nothing
        im = Image.open(thumbs.display(photo['path'], photo['rotation'], photo['enhanced']))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    im.convert('RGB').save(f"{target}.part", 'JPEG', quality=JPEG_QUALITY, exif=_exif(Image, photo))
    os.replace(f"{target}.part", target)


def _photo_file(thumbs, Image, photo, files, content, made):
    """
    The photo's own file in its month's folder, unless it's there already from the same file
    turned and enhanced the same way
    """
    target = os.path.join(content, photo['file'])
    stat = os.stat(photo['path'])
    signature = [photo['path'], stat.st_mtime_ns, stat.st_size, files]
    if files == JPEG:
        signature += [photo['rotation'], photo['enhanced'], photo['taken']]
    if made.get(photo['file']) == signature and os.path.exists(target):
        return
    if files == JPEG:
        _jpeg(thumbs, Image, photo, target)
    else:
        _copy(photo['path'], target)
    # so that a folder of them sorts by when they were taken, wherever it's copied to
    if photo['mtime'] is not None:
        os.utime(target, (photo['mtime'], photo['mtime']))
    made[photo['file']] = signature


def _site_copy(make, target):
    # the site's own copies are named by the look they have, so one that's there is right
    if not os.path.exists(target):
        _copy(make(), target)


def _people_faces(thumbs, spec, content, kept):
    faces = []
    for number, person in enumerate(spec['people']):
        face = person['face']
        relative = f"site/people/{number}.jpg"
        try:
            if face['box']:
                made = thumbs.face(face['path'], face['rotation'], face['box'], face['face_id'])
            else:
                made = thumbs.thumbnail(face['path'], face['rotation'])
            _copy_small(made, os.path.join(content, relative))
            kept.add(os.path.join(content, relative))
            faces.append(relative)
        except Exception:
            # such as nobody's face being found, only tagged by hand
            faces.append(None)
    return faces


def _write(path, text):
    # left alone when it's the same, as for _copy_small
    if _same(path, text.encode('utf-8')):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(f"{path}.part", 'w', encoding='utf-8') as fh:
        fh.write(text)
    os.replace(f"{path}.part", path)


def _zip_name(spec):
    return f"{spec['slug']}.zip"


def _write_site(spec, root, content, shown, faces, kept):
    counts = [0] * len(spec['people'])
    for photo in shown:
        for number in photo['people']:
            counts[number] += 1
    data = {
        'title': spec['title'],
        'made': time.strftime('%B %-d, %Y'),
        'people': [
            {'name': person['name'], 'face': face, 'photos': count}
            for person, face, count in zip(spec['people'], faces, counts)
        ],
        'photos': [
            {
                'key': photo['key'],
                'file': photo['file'],
                'date': photo['date'],
                'when': photo['when'],
                'people': photo['people'],
                'place': photo['place']
            }
            for photo in shown
        ],
        # beside the photos, for downloading them all at once
        'zip': _zip_name(spec) if spec.get('zip') else None
    }
    # escaped to ASCII, as a file's name on disk needn't be valid text
    script = f"window.EXPORT = {json.dumps(data)};\n"
    _write(os.path.join(content, 'site', 'data.js'), script)
    kept.add(os.path.join(content, 'site', 'data.js'))
    # the version makes browsers fetch the site again once it's changed.  From what's in it, not
    # when it was made, so that making it again with nothing new changes nothing
    digest = hashlib.sha1(script.encode())
    for directory, name in SITE_FILES:
        with open(os.path.join(directory, name), 'rb') as fh:
            digest.update(fh.read())
    version = digest.hexdigest()[:12]
    with open(os.path.join(SITE_DIR, 'index.html'), encoding='utf-8') as fh:
        page = fh.read()
    page = page.replace('{{title}}', html.escape(spec['title'])).replace('{{version}}', version)
    _write(os.path.join(root, 'index.html'), page)
    kept.add(os.path.join(root, 'index.html'))
    for directory, name in SITE_FILES:
        _copy_small(os.path.join(directory, name), os.path.join(root, 'site', name))
        kept.add(os.path.join(root, 'site', name))
    # all the page knows before a password: how to work out the folder from one
    lock = {'salt': spec['salt'], 'iterations': spec['iterations']} if spec.get('secret') else None
    settings = os.path.join(root, 'site', 'settings.js')
    _write(settings, f"window.EXPORT_SITE = {json.dumps({'version': version, 'lock': lock})};\n")
    kept.add(settings)


def _move_content(root, content):
    """
    Moves what an earlier run made to where this one's password, or having none, puts it, so that
    changing the password copies nothing again.  Whatever doesn't move goes when the run tidies up
    """
    earlier = [root] + [
        os.path.join(root, name) for name in os.listdir(root)
        if SECRET_FOLDER.fullmatch(name) and os.path.isdir(os.path.join(root, name))
    ]
    for source in earlier:
        if source == content:
            continue
        for part in CONTENT:
            old, new = os.path.join(source, part), os.path.join(content, part)
            if os.path.exists(old) and not os.path.exists(new):
                os.makedirs(os.path.dirname(new), exist_ok=True)
                os.rename(old, new)


def _tidy(root, kept):
    """
    Deletes what an earlier run left that this one didn't make, such as photos since hidden, an
    earlier password's folder, and the folders that leaves empty
    """
    for directory, _, names in os.walk(root, topdown=False):
        for name in names:
            full = os.path.join(directory, name)
            if full not in kept:
                os.remove(full)
        if directory != root and not os.listdir(directory):
            os.rmdir(directory)


def _zip(spec, content, progress, progress_path, stopping):
    """
    The photos as a zip, beside them, unless the one there already has them as they are now: a
    folder named after the export with the years in it and the months in those, and nothing of the
    website.  Stored, not compressed, as photos hardly compress.  Returns how it was left
    """
    target = os.path.join(content, _zip_name(spec))
    photos = os.path.join(content, 'photos')
    files = []
    for directory, directories, names in os.walk(photos):
        directories.sort()
        for name in sorted(names):
            if not name.endswith('.part'):
                full = os.path.join(directory, name)
                stat = os.stat(full)
                files.append((full, os.path.relpath(full, photos), stat.st_size, stat.st_mtime_ns))
    signature = hashlib.sha1(json.dumps([file[1:] for file in files]).encode('utf-8', 'surrogateescape')).hexdigest()
    if spec.get('zip_signature') == signature and os.path.exists(target):
        return signature
    progress.update(phase='zipping', started=int(time.time()), done=0, total=sum(file[2] for file in files))
    worklog.save_json(progress_path, progress)
    saved = time.time()
    # timestamps before 1980, which zip can't hold, become 1980
    with zipfile.ZipFile(f"{target}.part", 'w', zipfile.ZIP_STORED, allowZip64=True, strict_timestamps=False) as archive:
        for full, relative, size, _ in files:
            if stopping:
                break
            info = zipfile.ZipInfo.from_file(full, os.path.join(spec['slug'], relative), strict_timestamps=False)
            with open(full, 'rb') as source, archive.open(info, 'w', force_zip64=True) as written:
                shutil.copyfileobj(source, written, 1 << 20)
            progress['done'] += size
            if time.time() - saved > 2:
                worklog.save_json(progress_path, progress)
                saved = time.time()
    if stopping:
        os.remove(f"{target}.part")
        return None
    os.replace(f"{target}.part", target)
    return signature


def _size(root):
    return sum(os.path.getsize(os.path.join(directory, name)) for directory, _, names in os.walk(root) for name in names)


def main(queue_path, spec_path, progress_path, lock_path):
    # only one at a time
    lock = worklog.hold_lock(lock_path)
    if lock is None:
        return

    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    # only now, so that the lock shows this is running during these slow imports
    import thumbs
    from PIL import Image

    os.nice(10)
    with open(spec_path) as fh:
        spec = json.load(fh)
    with open(queue_path) as fh:
        queue = [json.loads(line) for line in fh]
    root = spec['folder']
    content = os.path.join(root, spec['secret']) if spec.get('secret') else root
    os.makedirs(content, exist_ok=True)
    _move_content(root, content)
    # what each photo's file was made from, kept beside the folder rather than shared in it
    made_path = f"{spec_path[:-len('.json')]}.files.json"
    try:
        with open(made_path) as fh:
            made = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        made = {}
    progress = {
        'pid': os.getpid(),
        'slug': spec['slug'],
        'state': 'running',
        'started': int(time.time()),
        'finished': None,
        'total': len(queue),
        'done': 0,
        'failed': 0
    }
    worklog.save_json(progress_path, progress)

    kept = set()
    faces = _people_faces(thumbs, spec, content, kept)
    shown = []
    for photo in queue:
        if stopping:
            break
        try:
            _photo_file(thumbs, Image, photo, spec['files'], content, made)
            for kind, make in (('thumbs', thumbs.thumbnail), ('large', thumbs.display)):
                target = os.path.join(content, 'site', kind, f"{photo['key']}.jpg")
                _site_copy(lambda: make(photo['path'], photo['rotation'], photo['enhanced']), target)
                kept.add(target)
            kept.add(os.path.join(content, photo['file']))
            shown.append(photo)
        except Exception:
            # such as a file that's gone or can't be decoded: tried again next time
            progress['failed'] += 1
        progress['done'] += 1
        if progress['done'] % SAVE_EVERY == 0:
            worklog.save_json(made_path, made)
            worklog.save_json(progress_path, progress)

    _write_site(spec, root, content, shown, faces, kept)
    zipped = spec.get('zip_signature')
    if not stopping:
        if spec.get('zip'):
            kept.add(os.path.join(content, _zip_name(spec)))
        _tidy(root, kept)
        made = {file: signature for file, signature in made.items() if os.path.join(content, file) in kept}
        worklog.save_json(made_path, made)
        if spec.get('zip'):
            zipped = _zip(spec, content, progress, progress_path, stopping)
    worklog.save_json(made_path, made)
    state = 'stopped' if stopping else 'finished'
    with open(spec_path) as fh:
        spec = json.load(fh)
    spec.update(state=state, finished=int(time.time()), photos=len(shown), failed=progress['failed'], bytes=_size(root), zip_signature=zipped)
    worklog.save_json(spec_path, spec)
    progress.update(state=state, finished=int(time.time()))
    worklog.save_json(progress_path, progress)


if __name__ == '__main__':
    main(*sys.argv[1:])
