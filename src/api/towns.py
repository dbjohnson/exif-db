"""
GeoNames' towns of 1,000 or more people (CC BY 4.0), from scripts/fetch_places.py: the nearest one to
a point, and those with a name.  Imports nothing from the api package, since scripts/index_locations.py
loads this by itself without starting a web app.
"""
import csv
import math
import os

import numpy as np

CITIES_CSV = os.getenv('PLACES_FILE', '/opt/places/cities.csv')


class Towns:
    def __init__(self, path=CITIES_CSV):
        with open(path, newline='', encoding='utf-8') as fh:
            rows = list(csv.DictReader(fh))
        # as city, county, state, country code, the same as in locations.csv
        self.names = [(row['name'], row['admin2'], row['admin1'], row['cc']) for row in rows]
        self.points = [(float(row['lat']), float(row['lon'])) for row in rows]
        self.lats = np.radians([lat for lat, _ in self.points])
        self.lons = np.radians([lon for _, lon in self.points])
        self.folded = [row['name'].casefold() for row in rows]
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

    def named(self, name, limit):
        """
        Towns whose names start with this, those named exactly this first, as (city, county, state,
        country code, lat, lon)
        """
        wanted = name.casefold().strip()
        if not wanted:
            return []
        exact, starting = [], []
        for place, folded in enumerate(self.folded):
            if folded == wanted:
                exact.append(place)
            elif folded.startswith(wanted):
                starting.append(place)
        return [(*self.names[place], *self.points[place]) for place in (exact + starting)[:limit]]


_towns = None


def towns():
    # loaded the first time it's needed, as it takes a moment
    global _towns
    if _towns is None:
        _towns = Towns()
    return _towns
