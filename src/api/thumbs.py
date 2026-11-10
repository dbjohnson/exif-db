import hashlib
import math
import os
import threading
from io import BytesIO

import pyheif
import rawpy
from PIL import Image, ImageEnhance, ImageOps


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
# Enhancing a photo first stretches its tones, so that these shares of its pixels end up pure black
# and pure white.  It never moves black above, or white below, these levels (0 to 1), so that a
# misty or snowy photo isn't given hard contrast it never had
ENHANCE_BLACK_CLIP = 0.002
ENHANCE_WHITE_CLIP = 0.005
ENHANCE_MAX_BLACK = 0.06
ENHANCE_MIN_WHITE = 0.85
# Then it lifts the shadows, as a flash would fill them in, by up to ENHANCE_MAX_FILL.  How much
# depends on the share of the photo below ENHANCE_SHADOW: none for a photo with none, and the most
# for one with ENHANCE_FULL_FILL or more.  A backlit face gets the most
ENHANCE_SHADOW = 64
ENHANCE_FULL_FILL = 0.4
ENHANCE_MAX_FILL = 1.0
# Then it lightens a photo that's still dark overall towards this average brightness, by a gamma
# of at most ENHANCE_MAX_GAMMA, and makes its colors this much stronger.  Tried on photos from
# across the library, CLAHE (local contrast) brought out more in the shadows but made faces gritty
ENHANCE_BRIGHTNESS = 0.45
ENHANCE_MAX_GAMMA = 0.75
ENHANCE_SATURATION = 1.2
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


def _level(histogram, share):
    # the brightness, from 0 to 1, with this share of the pixels darker than it
    target = share * sum(histogram)
    seen = 0
    for level, count in enumerate(histogram):
        seen += count
        if seen > target:
            return level / 255
    return 1


def enhance(im):
    """
    A photo made brighter and more colorful, by a curve worked out from the photo itself.  The same
    curve goes on each color, so that colors keep their hue
    """
    im = im.convert('RGB')
    histogram = im.convert('L').histogram()
    total = sum(histogram)
    black = min(_level(histogram, ENHANCE_BLACK_CLIP), ENHANCE_MAX_BLACK)
    white = max(_level(histogram, 1 - ENHANCE_WHITE_CLIP), ENHANCE_MIN_WHITE)
    fill = ENHANCE_MAX_FILL * min(sum(histogram[:ENHANCE_SHADOW]) / total / ENHANCE_FULL_FILL, 1)
    curve = []
    for level in range(256):
        value = min(max((level / 255 - black) / (white - black), 0), 1)
        # most at a third of the way up, and none at black or white, so nothing crosses over
        curve.append(value + fill * value * (1 - value) ** 2)
    average = sum(count * curve[level] for level, count in enumerate(histogram)) / total
    if 0 < average < ENHANCE_BRIGHTNESS:
        gamma = max(math.log(ENHANCE_BRIGHTNESS) / math.log(average), ENHANCE_MAX_GAMMA)
        curve = [value ** gamma for value in curve]
    lookup = [round(255 * value) for value in curve]
    return ImageEnhance.Color(im.point(lookup * 3)).enhance(ENHANCE_SATURATION)


def _adjust(im, rotation, enhanced):
    return _rotate(enhance(im) if enhanced else im, rotation)


def synology_folder(path):
    # where Synology keeps what it made from a photo or video
    folder, filename = os.path.split(path)
    return os.path.join(folder, '@eaDir', filename)


def synology_file(path, name, at=None):
    """
    The path to a file Synology made from a photo or video, unless there isn't one yet or the
    original has changed since it was made.  at is where the photo was in the library, for one
    that's been moved to the trash: Synology's copies stay behind there
    """
    copy = os.path.join(synology_folder(at or path), name)
    try:
        return copy if os.stat(copy).st_mtime_ns >= os.stat(path).st_mtime_ns else None
    except OSError:
        return None


def _synology_copy(path, name, at=None):
    return synology_file(path, f"SYNOPHOTO_THUMB_{name}.jpg", at)


def _cache_file(path, variant, at=None):
    # keyed on where a photo is in the library, so that it's the same once moved to the trash,
    # which keeps its modification time
    source = f"{at or path}:{os.stat(path).st_mtime_ns}{variant}"
    name = hashlib.sha1(source.encode('utf-8', 'surrogateescape')).hexdigest()
    return os.path.join(CACHE_DIR, name[:2], f"{name}.jpg")


def _cached(path, variant, quality, make, at=None):
    """
    Returns the path to a cached JPEG made from an image, creating it if needed.  make runs holding
    _decoding, so it mustn't get here again: two at once would each wait for the other for good
    """
    cached = _cache_file(path, variant, at)
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


def thumbnail(path, rotation=0, enhanced=False, at=None):
    copy = _synology_copy(path, SYNOLOGY_THUMB, at)
    if copy and not rotation and not enhanced:
        return copy

    def make():
        im = Image.open(copy) if copy else _decode(path, THUMB_SIZE)
        im.thumbnail((THUMB_SIZE, THUMB_SIZE))
        return _adjust(im, rotation, enhanced)

    return _cached(path, _thumb_variant(rotation, enhanced), 80, make, at)


def video_poster(path, size, rotation=0):
    # there's nothing here that can read a video, so without Synology's there's no picture
    poster = _synology_copy(path, size)
    if not poster:
        raise FileNotFoundError(path)
    if not rotation:
        return poster
    return _cached(path, f":poster:{size}:{rotation}", 85, lambda: _rotate(Image.open(poster), rotation))


def display(path, rotation=0, enhanced=False, at=None):
    """
    A copy for showing on screen, never the original: browsers can't show every format
    the library holds, and full size photos use up the frame's memory
    """
    copy = _synology_copy(path, SYNOLOGY_DISPLAY, at)
    if copy and not rotation and not enhanced:
        return copy

    def make():
        im = Image.open(copy) if copy else limit_size(_decode(path, max_image_size()))
        return _adjust(im, rotation, enhanced)

    return _cached(path, _display_variant(rotation, enhanced), 85, make, at)


def developed(path, rotation=0, enhanced=False):
    """
    A raw file developed from what its sensor recorded, at the size of a copy for the screen.  The
    preview inside a raw file can be much smaller than the photo, like an older Canon's
    """
    def make():
        with rawpy.imread(path) as raw:
            # half the size is still big enough for the screen, and much faster
            half = max(raw.sizes.width, raw.sizes.height) >= 2 * max_image_size()
            im = Image.fromarray(raw.postprocess(use_camera_wb=True, half_size=half))
        return _adjust(limit_size(im), rotation, enhanced)

    return _cached(path, _developed_variant(rotation, enhanced), 85, make)


# an enhanced copy is cached next to the plain one, so that turning enhancing off is instant
def _enhanced_variant(enhanced):
    return ':enhanced' if enhanced else ''


def _thumb_variant(rotation, enhanced=False):
    return (f":{rotation}" if rotation else '') + _enhanced_variant(enhanced)


def _display_variant(rotation, enhanced=False):
    return f":display:{rotation}" + _enhanced_variant(enhanced)


def _developed_variant(rotation, enhanced=False):
    return f":developed:{rotation}" + _enhanced_variant(enhanced)


def forget(path, at):
    """
    Delete the thumbnails and screen-sized copies made from a photo in the trash, at any rotation,
    enhanced or not.  Not pictures of its faces, which are only found by face
    """
    for rotation in CLOCKWISE.keys() | {0}:
        for enhanced in (False, True):
            for variant in (_thumb_variant(rotation, enhanced), _display_variant(rotation, enhanced), _developed_variant(rotation, enhanced)):
                try:
                    os.remove(_cache_file(path, variant, at))
                except FileNotFoundError:
                    pass


def face(path, rotation, box, face_id):
    """
    A picture of a face, given where it was found.  From the same copy of the photo it
    was found in, since the position is only right for that one
    """
    variant = f":face:{face_id}"
    cached = _cache_file(path, variant)
    if os.path.exists(cached):
        return cached
    # first, as making this can itself need _decoding
    copy = display(path, rotation)

    def make():
        with Image.open(copy) as im:
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

    return _cached(path, variant, 85, make)
