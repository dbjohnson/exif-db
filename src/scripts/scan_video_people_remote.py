"""
Finds the faces in every video from a faster computer than the server, the first time, since the NAS
would take days.  Reads frames straight from the running app, which OpenCV can seek in over HTTP, and
writes them to a file of its own.  Safe to stop and run again: it carries on from what's already in
that file.  Create a file called STOP next to it to stop between videos.

The file is then added to the live people.db, which keeps the names it already has, with
people_db.import_videos, and the people named by people_db.name_videos.

Needs numpy, onnxruntime and opencv-python-headless, and the models unpacked somewhere.

Usage: scan_video_people_remote.py <app url> <videos.db> <models folder> [--processes 4] [--threads 2]
"""
import argparse
import json
import multiprocessing
import os
import sys
import time
import urllib.parse
import urllib.request

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'api'))
import people_db
from face_model import FaceModel
from video_faces import faces_in_video

REPORT_EVERY = 50
_model = None
_app = None


def start_worker(app, models, threads):
    global _model, _app
    cv2.setNumThreads(threads)
    _model = FaceModel(threads, models)
    _app = app


def look_at(video):
    address = f"{_app}/api/movie{urllib.parse.quote(video['path'], errors='surrogateescape')}"
    try:
        return video, faces_in_video(_model, address, video['duration'], video['rotation']), None
    except Exception as error:
        return video, None, repr(error)


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
    with urllib.request.urlopen(f"{args.app}/api/movies", timeout=300) as response:
        videos = json.load(response)
    done = people_db.scanned_videos(connection)
    queue = [video for video in videos if video['id'] not in done]
    print(f"{len(videos):,} videos, {len(done):,} already looked at, {len(queue):,} to go", flush=True)
    started = time.time()
    finished = failed = faces = 0
    with multiprocessing.Pool(args.processes, start_worker, (args.app, args.models, args.threads)) as pool:
        for video, found, error in pool.imap_unordered(look_at, queue):
            if error:
                failed += 1
                print(f"failed: {video['path']!r}: {error}", flush=True)
            else:
                people_db.save_video(connection, video, found)
                finished += 1
                faces += len(found)
            if (finished + failed) % REPORT_EVERY == 0:
                rate = (finished + failed) / (time.time() - started)
                left = (len(queue) - finished - failed) / rate / 60
                print(f"{finished:,} of {len(queue):,}, {faces:,} faces, {failed} failed, {rate:.2f} videos/s, about {left:.0f} min left", flush=True)
            if os.path.exists(stop):
                print('stopped by request', flush=True)
                pool.terminate()
                break
    print(f"done: {finished:,} looked at, {faces:,} faces, {failed} failed, {(time.time() - started) / 60:.0f} min", flush=True)


if __name__ == '__main__':
    main()
