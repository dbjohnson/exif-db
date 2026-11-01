#!/usr/bin/env python3
"""
Keeps locations.csv up to date with where the photos and videos in exif.csv were taken.  Run at the
end of each scan, and safe to stop at any time: it carries on from what's already in the file.

Location needs tags the index doesn't collect, and adding those to tags.txt would misalign rows
appended to an existing index until a full rebuild.  So it has its own file, where only files not
seen before need reading.  Files with no location get a row too, so that they aren't read again.
"""
import csv
import math
import os
import subprocess
import sys
import tempfile

import numpy as np

STORAGE = os.getenv('LOCATIONS_STORAGE', '/storage')
EXIF_CSV = f'{STORAGE}/exif.csv'
LOCATIONS_CSV = f'{STORAGE}/locations.csv'
CITIES_CSV = os.getenv('PLACES_FILE', '/opt/places/cities.csv')
FIELDS = ('SourceFile', 'lat', 'lon', 'city', 'county', 'state', 'country')
BATCH = 500


def read_rows(path):
    with open(path, newline='', encoding='utf-8', errors='surrogateescape') as fh:
        yield from csv.DictReader(fh)


def to_read():
    csv.field_size_limit(sys.maxsize)
    # a rescanned file appears again further down, which makes no difference here
    wanted = {
        row['SourceFile']: None
        for row in read_rows(EXIF_CSV)
        if row.get('SourceFile') and (row.get('MIMEType') or '').startswith(('image/', 'video/'))
    }
    if os.path.exists(LOCATIONS_CSV):
        for row in read_rows(LOCATIONS_CSV):
            wanted.pop(row['SourceFile'], None)
    return list(wanted)


class Places:
    def __init__(self):
        rows = list(read_rows(CITIES_CSV))
        self.names = [(row['name'], row['admin2'], row['admin1'], row['cc']) for row in rows]
        self.lats = np.radians([float(row['lat']) for row in rows])
        self.lons = np.radians([float(row['lon']) for row in rows])
        self.known = {}

    def nearest(self, lat, lon):
        # to about a kilometre, as many photos from one place share a town
        key = (round(lat, 2), round(lon, 2))
        if key not in self.known:
            lat_radians, lon_radians = math.radians(lat), math.radians(lon)
            # the difference across the date line is the short way round
            across = np.abs(self.lons - lon_radians)
            across = np.minimum(across, 2 * math.pi - across) * math.cos(lat_radians)
            self.known[key] = self.names[int(np.argmin(across ** 2 + (self.lats - lat_radians) ** 2))]
        return self.known[key]


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_batch(paths):
    """
    What exiftool says about these, as {path: (lat, lon)}.  It exits non-zero if any one file is
    unreadable, while still reporting the rest
    """
    with tempfile.NamedTemporaryFile('w', suffix='.txt', encoding='utf-8', errors='surrogateescape') as listing:
        listing.write('\n'.join(paths))
        listing.flush()
        output = subprocess.run(
            ['exiftool', '-n', '-csv', '-fast', '-GPSLatitude', '-GPSLongitude', '-@', listing.name],
            capture_output=True
        ).stdout
    found = {}
    for row in csv.DictReader(output.decode('utf-8', 'surrogateescape').splitlines()):
        lat, lon = number(row.get('GPSLatitude')), number(row.get('GPSLongitude'))
        # exactly 0, 0 is a camera with no fix, rather than the Gulf of Guinea
        if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180 and (lat or lon):
            found[row['SourceFile']] = (lat, lon)
    return found


def main():
    paths = to_read()
    if not paths:
        return
    places = Places()
    is_new = not os.path.exists(LOCATIONS_CSV)
    done = located = 0
    with open(LOCATIONS_CSV, 'a', newline='', encoding='utf-8', errors='surrogateescape') as fh:
        writer = csv.writer(fh, lineterminator='\n')
        if is_new:
            writer.writerow(FIELDS)
        for start in range(0, len(paths), BATCH):
            part = paths[start:start + BATCH]
            found = read_batch(part)
            for path in part:
                if path in found:
                    lat, lon = found[path]
                    writer.writerow((path, f'{lat:.5f}', f'{lon:.5f}', *places.nearest(lat, lon)))
                    located += 1
                else:
                    writer.writerow((path, '', '', '', '', '', ''))
            fh.flush()
            done += len(part)
            print(f'{done:,} of {len(paths):,}, {located:,} with a location', flush=True)


if __name__ == '__main__':
    main()
