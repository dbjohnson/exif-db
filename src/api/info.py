"""
What's known about a photo, in plain words for the info card.  The index holds exiftool's numbers
rather than its descriptions, since it's read with -n.
"""
import datetime
import os

from api import exif, people, places

METERING = {1: 'Average', 2: 'Center-weighted', 3: 'Spot', 4: 'Multi-spot', 5: 'Multi-segment', 6: 'Partial'}
PROGRAM = {1: 'Manual', 2: 'Program', 3: 'Aperture priority', 4: 'Shutter priority', 5: 'Creative', 6: 'Action', 7: 'Portrait', 8: 'Landscape'}
EXPOSURE_MODE = {0: 'Auto', 1: 'Manual', 2: 'Auto bracket'}
WHITE_BALANCE = {0: 'Auto', 1: 'Manual'}
COLOR_SPACE = {1: 'sRGB', 2: 'Adobe RGB', 65535: 'Uncalibrated'}


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _text(value):
    if value is None or value != value:
        return None
    text = str(value).strip()
    return text or None


def _trim(number):
    return f"{number:.1f}".rstrip('0').rstrip('.')


def shutter(row):
    seconds = _number(row.get('ExposureTime')) or _number(row.get('ShutterSpeed'))
    if not seconds or seconds <= 0:
        return None
    return f"1/{round(1 / seconds)} s" if seconds < 1 else f"{_trim(seconds)} s"


def aperture(row):
    value = _number(row.get('FNumber')) or _number(row.get('Aperture'))
    return f"f/{_trim(value)}" if value else None


def focal_length(row):
    value = _number(row.get('FocalLength'))
    if not value:
        return None
    full_frame = _number(row.get('FocalLength35efl'))
    same = not full_frame or round(full_frame) == round(value)
    return f"{_trim(value)} mm" if same else f"{_trim(value)} mm ({round(full_frame)} mm equivalent)"


def compensation(row):
    value = _number(row.get('ExposureCompensation'))
    return f"{value:+.1f} EV" if value else None


def flash(row):
    value = _number(row.get('Flash'))
    return None if value is None else ('Fired' if int(value) & 1 else 'Did not fire')


def named(table, value):
    number = _number(value)
    return table.get(int(number)) if number is not None else None


def size(row):
    bytes_ = _number(row.get('FileSize'))
    if not bytes_:
        return None
    return f"{bytes_ / 1e6:.1f} MB" if bytes_ >= 1e6 else f"{round(bytes_ / 1e3)} KB"


def dimensions(row):
    width, height = _number(row.get('ImageWidth')), _number(row.get('ImageHeight'))
    if not width or not height:
        return None
    megapixels = width * height / 1e6
    return f"{int(width):,} × {int(height):,} ({_trim(megapixels)} MP)"


def when(value):
    try:
        taken = datetime.datetime.strptime(str(value)[:19], '%Y:%m:%d %H:%M:%S')
    except ValueError:
        return None
    return f"{taken:%A}, {taken:%B} {taken.day}, {taken.year}, {taken.hour % 12 or 12}:{taken:%M %p}"


def _section(title, *fields):
    shown = [{'label': label, 'value': value} for label, value in fields if value]
    return {'title': title, 'fields': shown} if shown else None


def photo_info(path):
    """
    The card's sections, its location, who's in it and every tag the index holds, or None if the
    index doesn't have this photo
    """
    row = exif.photo_details(path)
    if row is None:
        return None
    camera = ' '.join(part for part in (_text(row.get('Make')), _text(row.get('Model'))) if part)
    location = places.location_of(path)
    faces = [face['name'] for face in people.photo_faces(row['photo_id']) if face['name']]
    sections = [
        _section(
            'Taken',
            ('Date', when(row.get('DateTimeOriginal'))),
            ('Set by hand', 'Yes' if row.get('date_assigned') else None)
        ),
        _section(
            'Camera',
            ('Model', camera),
            ('Lens', _text(row.get('LensID')))
        ),
        _section(
            'Exposure',
            ('Shutter', shutter(row)),
            ('Aperture', aperture(row)),
            ('ISO', _text(row.get('ISO'))),
            ('Focal length', focal_length(row)),
            ('Compensation', compensation(row)),
            ('Flash', flash(row)),
            ('Mode', named(EXPOSURE_MODE, row.get('ExposureMode'))),
            ('Program', named(PROGRAM, row.get('ExposureProgram'))),
            ('Metering', named(METERING, row.get('MeteringMode'))),
            ('White balance', named(WHITE_BALANCE, row.get('WhiteBalance')))
        ),
        _section(
            'File',
            ('Name', _text(row.get('FileName'))),
            ('Folder', os.path.relpath(os.path.dirname(path), exif.LIBRARY_ROOT)),
            ('Dimensions', dimensions(row)),
            ('Size', size(row)),
            ('Type', _text(row.get('FileType'))),
            ('Color space', named(COLOR_SPACE, row.get('ColorSpace')))
        )
    ]
    return {
        'sections': [section for section in sections if section],
        'location': location,
        'people': sorted(set(faces)),
        'all': {
            tag: str(value)
            for tag, value in sorted(row.items())
            if tag not in ('photo_id', 'last_modified', 'date_assigned', 'review_status', 'favorite', 'rotation') and _text(value)
        }
    }
