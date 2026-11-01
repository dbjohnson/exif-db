import glob
import os
import shutil


DELETED_DIR = '/storage/deleted'


def _trashed(path):
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
            os.makedirs(os.path.dirname(_trashed(f)), exist_ok=True)
            shutil.move(f, _trashed(f))
            moved.append(f)
    return moved


def restore(files):
    """
    Move files back from the trash, leaving alone any whose place has been taken.
    Returns the files that were moved
    """
    restored = []
    for f in files:
        if os.path.exists(_trashed(f)) and not os.path.exists(f):
            os.makedirs(os.path.dirname(f), exist_ok=True)
            shutil.move(_trashed(f), f)
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
