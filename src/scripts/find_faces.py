"""
Works through a queue of photos, noting which have faces in them.  Started by api/faces.py, and
safe to stop at any time: what's been found is kept, and the next run carries on from there.

Usage: find_faces.py <queue> <results> <progress> <lock>
"""
import json
import os
import signal
import sys
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL = os.path.join(APP_DIR, 'models', 'face_detection_yunet_2023mar.onnx')
# only these: importing the api package would also start a web app
sys.path.insert(0, os.path.join(APP_DIR, 'api'))
import worklog  # noqa: E402

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
    worklog.append_log(path, [(photo_id, encode(faces)) for photo_id, faces in rows])


def main(queue_path, results_path, progress_path, lock_path):
    # only one at a time
    lock = worklog.hold_lock(lock_path)
    if lock is None:
        return

    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    # only now, so that the lock shows this is running during these slow imports
    import cv2
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
    worklog.save_json(progress_path, progress)

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
            worklog.save_json(progress_path, progress)

    save_results(results_path, rows)
    progress.update(state='stopped' if stopping else 'finished', finished=int(time.time()))
    worklog.save_json(progress_path, progress)


if __name__ == '__main__':
    main(*sys.argv[1:])
