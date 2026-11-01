"""
Works through a queue of photos, noting which have faces in them.  Started by api/faces.py, and
safe to stop at any time: what's been found is kept, and the next run carries on from there.

Usage: find_faces.py <queue> <results> <progress> <lock>
"""
import csv
import fcntl
import json
import os
import signal
import sys
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL = os.path.join(APP_DIR, 'models', 'face_detection_yunet_2023mar.onnx')
# the same as in exif.py
NO_FACES = 'none'
MIN_SCORE = 0.6
MAX_OVERLAP = 0.3
MAX_FACES = 100
# leaves the NAS's other two cores for serving pages
THREADS = 2
# how many photos could be checked again if this is killed
SAVE_EVERY = 20


def detect(cv2, thumbs, detector, path, rotation):
    # a thumbnail, since Synology's are already upright and about 4x quicker than its larger copies
    image = cv2.imread(thumbs.thumbnail(path, rotation))
    if image is None:
        raise ValueError(f"Can't read a thumbnail for {path}")
    height, width = image.shape[:2]
    detector.setInputSize((width, height))
    _, found = detector.detect(image)
    if found is None:
        return []
    return [
        [
            float(face[0]) / width,
            float(face[1]) / height,
            float(face[2]) / width,
            float(face[3]) / height,
            float(face[-1])
        ]
        for face in found
    ]


def encode(faces):
    """
    "x y w h score;..." as fractions of the image, then the detector's confidence.  Nothing here
    needs quoting in a CSV, since a row cut short inside quotes would stop the whole log loading
    """
    return ';'.join(' '.join(f"{value:.4f}" for value in face) for face in faces) or NO_FACES


def save_results(path, rows):
    # the same format as the other logs in exif.py, whose worker may be reading this
    with open(path, 'a', newline='', encoding='utf-8', errors='surrogateescape') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        now = time.time_ns()
        writer = csv.writer(fh, lineterminator='\n')
        writer.writerows((photo_id, encode(faces), now) for photo_id, faces in rows)


def save_progress(path, progress):
    tmp = f"{path}.{os.getpid()}"
    with open(tmp, 'w') as fh:
        json.dump(progress, fh)
    os.replace(tmp, path)


def main(queue_path, results_path, progress_path, lock_path):
    # only one at a time; the lock goes with this process, however it ends
    lock = open(lock_path, 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return

    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    # only now, so that the lock shows this is running during these slow imports
    import cv2
    # only thumbs itself: importing the api package would also start a web app
    sys.path.insert(0, os.path.join(APP_DIR, 'api'))
    import thumbs

    os.nice(10)
    cv2.setNumThreads(THREADS)
    detector = cv2.FaceDetectorYN.create(MODEL, '', (320, 320), MIN_SCORE, MAX_OVERLAP, MAX_FACES)

    with open(queue_path) as fh:
        queue = [json.loads(line) for line in fh]
    progress = {
        'pid': os.getpid(),
        'state': 'running',
        'started': int(time.time()),
        'finished': None,
        'total': len(queue),
        'done': 0,
        'with_faces': 0,
        'failed': 0
    }
    save_progress(progress_path, progress)

    rows = []
    for photo in queue:
        if stopping:
            break
        try:
            faces = detect(cv2, thumbs, detector, photo['path'], photo['rotation'])
            rows.append((photo['id'], faces))
            progress['with_faces'] += bool(faces)
        except Exception:
            # such as a file that's gone or can't be decoded: tried again next run
            progress['failed'] += 1
        progress['done'] += 1
        if progress['done'] % SAVE_EVERY == 0:
            save_results(results_path, rows)
            rows = []
            save_progress(progress_path, progress)

    save_results(results_path, rows)
    progress.update(state='stopped' if stopping else 'finished', finished=int(time.time()))
    save_progress(progress_path, progress)


if __name__ == '__main__':
    main(*sys.argv[1:])
