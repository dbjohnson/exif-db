"""
Finds faces across the whole library from a faster computer than the server, the first time, since
a Celeron would take days.  Asks the running app for copies of photos, the same ones it shows, and
writes a people.db to be copied into the storage volume.  Safe to stop and run again: it carries on
from what's already in that file.  Create a file called STOP next to it to stop between photos.

Needs numpy, onnxruntime and opencv-python-headless, and the models unpacked somewhere.

Usage: scan_people_remote.py <app url> <people.db> <models folder> [--processes 4] [--threads 2]
"""
import argparse
import json
import multiprocessing
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'api'))
import people_db
from face_model import FaceModel

TIMEOUT_SECS = 300
ATTEMPTS = 3
REPORT_EVERY = 100
_model = None
_app = None


def fetch(app, path):
    for attempt in range(ATTEMPTS):
        try:
            with urllib.request.urlopen(app + path, timeout=TIMEOUT_SECS) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            # a photo that's gone or can't be decoded won't change by asking again
            if isinstance(error, urllib.error.HTTPError) and error.code < 500:
                raise
            if attempt == ATTEMPTS - 1:
                raise
            time.sleep(5 * (attempt + 1))


def photos_with_faces(app):
    """
    Everything the library shows as having faces, so no hidden photos or duplicates' other copies
    """
    page = fetch(app, '/library?with_faces=true').decode()
    urls = sorted(set(re.findall(r'href="(/library/[^"]+)"', page)))
    found = {}
    for url in urls:
        html = fetch(app, url).decode()
        match = re.search(r'const PHOTOS = (.*);\n', html)
        for photo in json.loads(match[1]) if match else []:
            found[photo['id']] = {'id': photo['id'], 'path': photo['path'], 'rotation': photo['rotation']}
    return list(found.values())


def start_worker(app, models, threads):
    global _model, _app
    cv2.setNumThreads(threads)
    _model = FaceModel(threads, models)
    _app = app


def look_at(photo):
    # the same copy of the photo the app will later cut this face from
    rotate = f"?rotate={photo['rotation']}" if photo['rotation'] else ''
    try:
        data = fetch(_app, f"/api/image{urllib.parse.quote(photo['path'], errors='surrogateescape')}{rotate}")
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("can't decode it")
        return photo, _model.faces(image), None
    except Exception as error:
        return photo, None, repr(error)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('app')
    parser.add_argument('database')
    parser.add_argument('models')
    parser.add_argument('--processes', type=int, default=4)
    parser.add_argument('--threads', type=int, default=2)
    args = parser.parse_args()
    stop = os.path.join(os.path.dirname(os.path.abspath(args.database)), 'STOP')
    connection = people_db.connect(args.database)
    photos = photos_with_faces(args.app)
    done = people_db.scanned_photos(connection)
    queue = [photo for photo in photos if photo['id'] not in done]
    print(f"{len(photos):,} photos with faces, {len(done):,} already looked at, {len(queue):,} to go", flush=True)
    started = time.time()
    finished = failed = faces = 0
    with multiprocessing.Pool(args.processes, start_worker, (args.app, args.models, args.threads)) as pool:
        for photo, found, error in pool.imap_unordered(look_at, queue):
            if error:
                failed += 1
                print(f"failed: {photo['path']!r}: {error}", flush=True)
            else:
                people_db.save_photo(connection, photo, found)
                finished += 1
                faces += len(found)
            if (finished + failed) % REPORT_EVERY == 0:
                rate = (finished + failed) / (time.time() - started)
                left = (len(queue) - finished - failed) / rate / 60
                print(f"{finished:,} of {len(queue):,}, {faces:,} faces, {failed} failed, {rate:.1f} photos/s, about {left:.0f} min left", flush=True)
            if os.path.exists(stop):
                print('stopped by request', flush=True)
                pool.terminate()
                break
    print(f"done: {finished:,} looked at, {faces:,} faces, {failed} failed, {(time.time() - started) / 60:.0f} min", flush=True)


if __name__ == '__main__':
    main()
