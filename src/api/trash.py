import glob
import os
import shutil

from api import thumbs
from api.exif import LIBRARY_ROOT


DELETED_DIR = '/storage/deleted'
# what the recycle bin shows, of the files deleted together as one photo
IMAGE_EXTENSIONS = {'JPG', 'JPEG', 'PNG', 'HEIC', 'HEIF', 'GIF', 'TIF', 'TIFF', 'WEBP', 'BMP', 'ARW', 'CR2', 'NEF', 'DNG'}
# and of those, which to show, in the order the index prefers them
SHOWN_FIRST = ('JPG', 'JPEG', 'PNG', 'HEIC')


def _extension(path):
    return os.path.splitext(path)[1][1:].upper()


def trashed(path):
    # keeps the file's whole path, since file names aren't unique across the library
    return os.path.join(DELETED_DIR, path.lstrip('/'))


def discard(path):
    """
    Move a photo to the trash, along with any files differing only by extension.
    Returns the files that were moved
    """
    return discard_files(glob.glob(f"{glob.escape(os.path.splitext(path)[0])}.*"))


def discard_files(files):
    """
    Move exactly these files to the trash, skipping any that have already gone.  Returns the
    files that were moved
    """
    moved = []
    for f in files:
        if os.path.exists(f):
            os.makedirs(os.path.dirname(trashed(f)), exist_ok=True)
            shutil.move(f, trashed(f))
            moved.append(f)
    return moved


def restore(files):
    """
    Move files back from the trash, leaving alone any whose place has been taken.
    Returns the files that were moved
    """
    restored = []
    for f in files:
        if os.path.exists(trashed(f)) and not os.path.exists(f):
            os.makedirs(os.path.dirname(f), exist_ok=True)
            shutil.move(trashed(f), f)
            restored.append(f)
    return restored


def summary():
    files = 0
    size = 0
    for folder, _, names in os.walk(DELETED_DIR):
        for name in names:
            files += 1
            size += os.path.getsize(os.path.join(folder, name))
    return {'files': files, 'bytes': size}


def empty():
    """
    Permanently delete everything in the trash
    """
    if os.path.exists(DELETED_DIR):
        shutil.rmtree(DELETED_DIR)


def library_photos():
    """
    The photos in the trash that were deleted from the library.  Each has the files deleted along
    with it, such as a Live Photo's video, the one to show, and when the last of them moved here,
    which only lasts until the trash is copied somewhere else.  Leaves out
    videos on their own, and anything put in the deleted folder by hand, which has no place in the
    library to go back to
    """
    by_photo = {}
    for folder, folders, names in os.walk(trashed(LIBRARY_ROOT)):
        folders[:] = [name for name in folders if name != '@eaDir']
        for name in names:
            original = '/' + os.path.relpath(os.path.join(folder, name), DELETED_DIR)
            # the same files as discard takes together
            by_photo.setdefault(os.path.splitext(original)[0], []).append(original)
    photos = []
    for files in by_photo.values():
        images = [f for f in files if _extension(f) in IMAGE_EXTENSIONS]
        if not images:
            continue
        shown = min(images, key=lambda f: (
            SHOWN_FIRST.index(_extension(f)) if _extension(f) in SHOWN_FIRST else len(SHOWN_FIRST),
            f
        ))
        try:
            # moving a file into the trash changes its ctime and nothing else does
            deleted = max(os.stat(trashed(f)).st_ctime for f in files)
        except FileNotFoundError:
            # emptied meanwhile
            continue
        photos.append({'path': shown, 'files': sorted(files), 'deleted': deleted})
    return photos


def purge(files):
    """
    Delete files in the trash for good, given by where they were in the library, along with the
    thumbnails made from them.  Also what Synology made from one, which stays behind in the
    library, unless another file has taken its place there.  Returns the files that were deleted
    """
    purged = []
    for f in files:
        path = trashed(f)
        if not os.path.isfile(path):
            continue
        thumbs.forget(path, f)
        os.remove(path)
        purged.append(f)
        if not os.path.exists(f):
            shutil.rmtree(thumbs.synology_folder(f), ignore_errors=True)
        _remove_empty_folders(os.path.dirname(path))
    return purged


def _remove_empty_folders(folder):
    while folder.startswith(f"{DELETED_DIR}/"):
        try:
            os.rmdir(folder)
        except OSError:
            # not empty
            return
        folder = os.path.dirname(folder)
