"""
Works through a queue of videos, finding the faces in frames of each and naming the people in them
who are clearly someone named in photos.  Started by api/app.py after a scan, and safe to stop at any
time: every video is saved as it's done, and the next run carries on from there.

Usage: find_video_people.py <queue> <people.db> <progress> <lock>
"""
import fcntl
import json
import os
import signal
import sys
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# leaves the NAS's other two cores for serving pages
THREADS = 2


def save_progress(path, progress):
    tmp = f"{path}.{os.getpid()}"
    with open(tmp, 'w') as fh:
        json.dump(progress, fh)
    os.replace(tmp, path)


def main(queue_path, database, progress_path, lock_path):
    # only one at a time; the lock goes with this process, however it ends
    lock = open(lock_path, 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return

    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    with open(queue_path) as fh:
        queue = [json.loads(line) for line in fh]
    progress = {
        'pid': os.getpid(),
        'state': 'running',
        'started': int(time.time()),
        'finished': None,
        'total': len(queue),
        'done': 0,
        'faces': 0,
        'failed': 0
    }
    save_progress(progress_path, progress)
    # only now, so that the lock shows this is running during these slow imports
    import cv2
    # only these, as importing the api package would also start a web app
    sys.path.insert(0, os.path.join(APP_DIR, 'api'))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import people_db
    from face_model import FaceModel
    from video_faces import faces_in_video

    os.nice(10)
    cv2.setNumThreads(THREADS)
    model = FaceModel(THREADS)
    connection = people_db.connect(database)
    done = []
    for video in queue:
        if stopping:
            break
        try:
            found = faces_in_video(model, video['path'], video['duration'], video['rotation'])
            people_db.save_video(connection, video, found)
            progress['faces'] += len(found)
            done.append(video['id'])
        except Exception:
            # such as a file that's gone or can't be decoded: tried again next run
            progress['failed'] += 1
        progress['done'] += 1
        # a video takes about a minute, so every one is worth reporting
        save_progress(progress_path, progress)
    progress['state'] = 'naming'
    save_progress(progress_path, progress)
    people_db.name_videos(connection, done)
    progress.update(state='stopped' if stopping else 'finished', finished=int(time.time()))
    save_progress(progress_path, progress)


if __name__ == '__main__':
    main(*sys.argv[1:])
