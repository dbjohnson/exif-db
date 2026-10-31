import hashlib
import os
import threading
from io import BytesIO

import pyheif
import rawpy
from PIL import Image, ImageOps


CACHE_DIR = '/storage/thumbs'
THUMB_SIZE = 480
RAW_EXTENSIONS = ('ARW', 'CR2', 'NEF')
CLOCKWISE = {
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90
}
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


def limit_size(im):
    # resize to max 2000px
    width, height = im.size
    ratio = int(os.getenv('MAX_IMAGE_SIZE', 2000)) / max(im.size)
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
    def make():
        im = _decode(path, THUMB_SIZE)
        im.thumbnail((THUMB_SIZE, THUMB_SIZE))
        return _rotate(im, rotation)

    return _cached(path, f":{rotation}" if rotation else '', 80, make)


def rotated(path, rotation):
    """
    A copy for display at the same size as the unrotated photo: originals are never changed
    """
    def make():
        im = _decode(path)
        return _rotate(limit_size(im) if _is_heic(path) else im, rotation)

    return _cached(path, f":full:{rotation}", 90, make)
