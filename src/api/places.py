"""
Where photos and movies were taken, going by scripts/index_locations.py's locations.csv, and what
the names of their folders say, such as "2002.03.11 - Escalante".  Old photos mostly have only the
folder to go on, and many phones don't record a location at all.
"""
import csv
import os
import re

from iso3166 import countries_by_alpha2

from api import exif, movies

LOCATIONS_CSV = '/storage/locations.csv'
CITY, STATE, COUNTRY, FOLDER, AREA = 'city', 'state', 'country', 'folder', 'area'
# the names people actually use, in place of the official ones
COUNTRY_NAMES = {'US': 'United States', 'GB': 'United Kingdom', 'RU': 'Russia', 'KR': 'South Korea'}
MAX_RESULTS = 200
# the photos and movies to show for an area of the map, out of however many are in it
AREA_PHOTOS = 60
AREA_MOVIES = 12
# how finely to share a circle on the map: about a hundred metres
POINT_DECIMALS = 3
BROWSE_CITIES = 60
# what comes before a folder's name: "2002.03.11 - ", "2008.10 ", "2003 - "
DATE_PREFIX = re.compile(r'^[\d._\- ]+')
_cache = {}


def country_name(code):
    if code in COUNTRY_NAMES:
        return COUNTRY_NAMES[code]
    country = countries_by_alpha2.get(code)
    return country.apolitical_name if country else code


def _locations():
    """
    Where each located file was taken, by its path
    """
    stamp = exif._file_stamps(LOCATIONS_CSV)
    if _cache.get('locations_stamp') != stamp:
        located = {}
        read = set()
        if os.path.exists(LOCATIONS_CSV):
            with open(LOCATIONS_CSV, newline='', encoding='utf-8', errors='surrogateescape') as fh:
                for row in csv.DictReader(fh):
                    read.add(row['SourceFile'])
                    # a row cut short by a crash has no place to go on either
                    if row.get('lat') and row.get('country') is not None:
                        located[row['SourceFile']] = row
        _cache.update(locations_stamp=stamp, located=located, read=read)
    return _cache['located']


def location_read(path):
    # whether its file has been looked at yet, as new files wait for the end of the next scan
    _locations()
    return path in _cache['read']


def location_of(path):
    row = _locations().get(path)
    if not row:
        return None
    return {
        'lat': float(row['lat']),
        'lon': float(row['lon']),
        'city': row['city'],
        'county': row['county'],
        'state': row['state'],
        'country': country_name(row['country'])
    }


def _folder_name(folder):
    # a folder whose name is only a date, like 2024/04, says nothing about where
    name = DATE_PREFIX.sub('', os.path.basename(folder)).strip()
    return name if re.search(r'[^\W\d_]', name) else None


def _entry_key(kind, *parts):
    return kind, '|'.join(parts)


def _index():
    """
    Every place and named folder, with how many photos and movies it has
    """
    stamp = (exif.shown_photos()[0], _cache.get('locations_stamp'), exif._file_stamps(movies.MOVIES_CSV))
    located = _locations()
    stamp = (stamp[0], _cache['locations_stamp'], stamp[2])
    if _cache.get('index_stamp') == stamp:
        return _cache['index']
    entries = {}

    def count(kind, key, label, detail, field):
        entry = entries.setdefault((kind, key), {'kind': kind, 'key': key, 'label': label, 'detail': detail, 'photos': 0, 'movies': 0})
        entry[field] += 1

    def add(path, field):
        row = located.get(path)
        if row:
            name = country_name(row['country'])
            state = row['state']
            count(COUNTRY, row['country'], name, '', field)
            if state:
                count(STATE, f"{state}|{row['country']}", state, name, field)
            if row['city']:
                detail = ', '.join(part for part in (state, name) if part)
                count(CITY, f"{row['city']}|{state}|{row['country']}", row['city'], detail, field)
        folder = os.path.dirname(path)
        name = _folder_name(folder)
        if name:
            count(FOLDER, folder, name, os.path.relpath(folder, exif.LIBRARY_ROOT), field)

    for path in exif.shown_paths():
        add(path, 'photos')
    for clip in movies.clips():
        add(clip['path'], 'movies')
    result = sorted(entries.values(), key=lambda entry: -(entry['photos'] + entry['movies']))
    _cache.update(index_stamp=stamp, index=result)
    return result


def search(query):
    """
    Places and folders whose names have this in them, largest first.  With nothing to search for,
    the places to browse: folders are too many and too various to be worth listing
    """
    index = _index()
    words = query.casefold().split()
    if not words:
        cities = [entry for entry in index if entry['kind'] == CITY][:BROWSE_CITIES]
        return [entry for entry in index if entry['kind'] in (COUNTRY, STATE)] + cities
    return [
        entry for entry in index
        if all(word in f"{entry['label']} {entry['detail']}".casefold() for word in words)
    ][:MAX_RESULTS]


def _box(key):
    # south, west, north, east
    try:
        south, west, north, east = map(float, key.split(','))
    except ValueError:
        return None
    return south, west, north, east


def describe(kind, key):
    if kind == AREA:
        return {'kind': AREA, 'key': key, 'label': 'On the map', 'detail': ''} if _box(key) else None
    return next((entry for entry in _index() if entry['kind'] == kind and entry['key'] == key), None)


def _shown_located():
    """
    What's on the map: located photos that aren't hidden, and movies, as (path, row, is it a movie)
    """
    located = _locations()
    shown = [(path, False) for path in exif.shown_paths()] + [(clip['path'], True) for clip in movies.clips()]
    return [(path, located[path], is_movie) for path, is_movie in shown if path in located]


def points():
    """
    Where things are, as [lat, lon, how many], with things close together sharing a point
    """
    stamp = (exif.shown_photos()[0], exif._file_stamps(LOCATIONS_CSV), exif._file_stamps(movies.MOVIES_CSV))
    if _cache.get('points_stamp') != stamp:
        counts = {}
        for _, row, _ in _shown_located():
            place = (round(float(row['lat']), POINT_DECIMALS), round(float(row['lon']), POINT_DECIMALS))
            counts[place] = counts.get(place, 0) + 1
        _cache.update(points_stamp=stamp, points=[[lat, lon, count] for (lat, lon), count in counts.items()])
    return _cache['points']


def bounds(kind, key):
    """
    The corners around a place's located files, or None if it has none
    """
    located = _locations()
    rows = [located[path] for path in paths_at(kind, key) if path in located]
    if not rows:
        return None
    lats, lons = [float(row['lat']) for row in rows], [float(row['lon']) for row in rows]
    return [[min(lats), min(lons)], [max(lats), max(lons)]]


def in_area(key):
    """
    How many photos and movies are in an area of the map, and the newest of each
    """
    paths = paths_at(AREA, key)
    photos = exif.photos_with_paths(paths) if paths else []
    clips = [clip for clip in reversed(movies.clips()) if clip['path'] in paths]
    return photos, clips


def paths_at(kind, key):
    """
    The located files at a place, or those in a folder
    """
    if kind == AREA:
        box = _box(key)
        if not box:
            return set()
        south, west, north, east = box
        return {
            path for path, row in _locations().items()
            if south <= float(row['lat']) <= north and west <= float(row['lon']) <= east
        }
    if kind == FOLDER:
        return {
            path
            for path in [*exif.shown_paths(), *(clip['path'] for clip in movies.clips())]
            if os.path.dirname(path) == key
        }
    parts = key.split('|')
    if kind == COUNTRY:
        match = lambda row: row['country'] == parts[0]
    elif kind == STATE:
        match = lambda row: (row['state'], row['country']) == tuple(parts)
    else:
        match = lambda row: (row['city'], row['state'], row['country']) == tuple(parts)
    return {path for path, row in _locations().items() if match(row)}


def coverage():
    return {'located': len(_locations())}
