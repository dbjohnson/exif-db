import hashlib
import os
import threading
from io import BytesIO

import pyheif
import rawpy
from PIL import Image, ImageOps


CACHE_DIR = '/storage/thumbs'
THUMB_SIZE = 480
FACE_SIZE = 240
# how much of what's around a face to show, as a share of the face's size
FACE_MARGIN = 0.25
RAW_EXTENSIONS = ('ARW', 'CR2', 'NEF')
CLOCKWISE = {
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90
}
# Synology's indexer keeps upright copies of each photo in @eaDir, which cost nothing to serve
SYNOLOGY_DISPLAY = 'XL'
SYNOLOGY_THUMB = 'B'
# a page of thumbnails is requested all at once, and full size decodes are slow and
# memory hungry on a NAS
_decoding = threading.BoundedSemaphore(2)


def read_heic(path):
    heif_file = pyheif.read(path)
    return Image.frombytes(
        heif_file.mode,
        heif_file.size,
        heif_file.data,
        "raw",
        heif_file.mode,
        heif_file.stride
    )


def max_image_size():
    return int(os.getenv('MAX_IMAGE_SIZE', 2000))


def limit_size(im):
    width, height = im.size
    ratio = max_image_size() / max(im.size)
    if ratio < 1:
        return im.resize((int(width * ratio), int(height * ratio)))
    return im


def _is_heic(path):
    return path.split('.')[-1].upper() == 'HEIC'


def _decode(path, size=None):
    extension = path.split('.')[-1].upper()
    if _is_heic(path):
        # libheif has already applied the rotation
        return read_heic(path)
    if extension in RAW_EXTENSIONS:
        with rawpy.imread(path) as raw:
            thumb = raw.extract_thumb()
        if thumb.format == rawpy.ThumbFormat.BITMAP:
            return Image.fromarray(thumb.data)
        im = Image.open(BytesIO(thumb.data))
    else:
        im = Image.open(path)
    if size:
        # JPEGs can be decoded at a fraction of full size, which is much faster
        im.draft('RGB', (size, size))
    # the orientation tag doesn't survive into the copy, so apply it now
    return ImageOps.exif_transpose(im)


def _rotate(im, rotation):
    return im.transpose(CLOCKWISE[rotation]) if rotation else im


def synology_file(path, name):
    """
    The path to a file Synology made from a photo or video, unless there isn't one yet or the
    original has changed since it was made
    """
    folder, filename = os.path.split(path)
    copy = os.path.join(folder, '@eaDir', filename, name)
    try:
        return copy if os.stat(copy).st_mtime_ns >= os.stat(path).st_mtime_ns else None
    except OSError:
        return None


def _synology_copy(path, name):
    return synology_file(path, f"SYNOPHOTO_THUMB_{name}.jpg")


def _cached(path, variant, quality, make):
    """
    Returns the path to a cached JPEG made from an image, creating it if needed
    """
    source = f"{path}:{os.stat(path).st_mtime_ns}{variant}"
    name = hashlib.sha1(source.encode('utf-8', 'surrogateescape')).hexdigest()
    cached = os.path.join(CACHE_DIR, name[:2], f"{name}.jpg")
    if os.path.exists(cached):
        return cached
    with _decoding:
        # another request may have made it while this one waited
        if os.path.exists(cached):
            return cached
        im = make()
        os.makedirs(os.path.dirname(cached), exist_ok=True)
        # other workers can be making this same file
        tmp = f"{cached}.{os.getpid()}"
        im.convert('RGB').save(tmp, 'JPEG', quality=quality)
        os.replace(tmp, cached)
    return cached


def thumbnail(path, rotation=0):
    copy = _synology_copy(path, SYNOLOGY_THUMB)
    if copy and not rotation:
        return copy

    def make():
        im = Image.open(copy) if copy else _decode(path, THUMB_SIZE)
        im.thumbnail((THUMB_SIZE, THUMB_SIZE))
        return _rotate(im, rotation)

    return _cached(path, f":{rotation}" if rotation else '', 80, make)


def video_poster(path, size):
    # there's nothing here that can read a video, so without Synology's there's no picture
    poster = _synology_copy(path, size)
    if not poster:
        raise FileNotFoundError(path)
    return poster


def display(path, rotation=0):
    """
    A copy for showing on screen, never the original: browsers can't show every format
    the library holds, and full size photos use up the frame's memory
    """
    copy = _synology_copy(path, SYNOLOGY_DISPLAY)
    if copy and not rotation:
        return copy

    def make():
        im = Image.open(copy) if copy else limit_size(_decode(path, max_image_size()))
        return _rotate(im, rotation)

    return _cached(path, f":display:{rotation}", 85, make)


def face(path, rotation, box, face_id):
    """
    A picture of a face, given where it was found.  From the same copy of the photo it
    was found in, since the position is only right for that one
    """
    def make():
        with Image.open(display(path, rotation)) as im:
            width, height = im.size
            margin_x, margin_y = box['Width'] * FACE_MARGIN, box['Height'] * FACE_MARGIN
            # a face can run off the edge of a photo
            cropped = im.crop((
                max(0, int((box['Left'] - margin_x) * width)),
                max(0, int((box['Top'] - margin_y) * height)),
                min(width, int((box['Left'] + box['Width'] + margin_x) * width)),
                min(height, int((box['Top'] + box['Height'] + margin_y) * height))
            ))
            cropped.thumbnail((FACE_SIZE, FACE_SIZE))
            return cropped

    return _cached(path, f":face:{face_id}", 85, make)
