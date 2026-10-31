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


def _decode(path):
    extension = path.split('.')[-1].upper()
    if extension == 'HEIC':
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
    # JPEGs can be decoded at a fraction of full size, which is much faster
    im.draft('RGB', (THUMB_SIZE, THUMB_SIZE))
    # the orientation tag doesn't survive into the thumbnail, so apply it now
    return ImageOps.exif_transpose(im)


def thumbnail(path):
    """
    Returns the path to a cached JPEG thumbnail of an image, creating it if needed
    """
    source = f"{path}:{os.stat(path).st_mtime_ns}"
    name = hashlib.sha1(source.encode('utf-8', 'surrogateescape')).hexdigest()
    cached = os.path.join(CACHE_DIR, name[:2], f"{name}.jpg")
    if os.path.exists(cached):
        return cached
    with _decoding:
        # another request may have made it while this one waited
        if os.path.exists(cached):
            return cached
        im = _decode(path)
        im.thumbnail((THUMB_SIZE, THUMB_SIZE))
        os.makedirs(os.path.dirname(cached), exist_ok=True)
        # other workers can be making this same file
        tmp = f"{cached}.{os.getpid()}"
        im.convert('RGB').save(tmp, 'JPEG', quality=80)
        os.replace(tmp, cached)
    return cached
