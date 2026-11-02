"""
Works through a queue of photos, finding the dogs and cats in each, and names those that are clearly a dog
already named.  Started by api/app.py after a scan, and safe to stop at any time: every photo is
saved as it's done, and the next run carries on from there.

Usage: find_dogs.py <queue> <people.db> <progress> <lock>
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
SAVE_EVERY = 5


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
        'dogs': 0,
        'named': 0,
        'failed': 0
    }
    save_progress(progress_path, progress)
    # only now, so that the lock shows this is running during these slow imports
    import cv2
    # only these, as importing the api package would also start a web app
    sys.path.insert(0, os.path.join(APP_DIR, 'api'))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import people_db
    import thumbs
    import dog_model

    os.nice(10)
    cv2.setNumThreads(THREADS)
    model = dog_model.DogModel(THREADS)
    connection = people_db.connect(database)
    found = []
    for photo in queue:
        if stopping:
            break
        try:
            image = cv2.imread(thumbs.display(photo['path'], photo['rotation']))
            if image is None:
                raise ValueError(f"Can't read {photo['path']}")
            found += dog_model.save_photo(connection, photo, model.animals(image))
            progress['dogs'] = len(found)
        except Exception:
            # such as a file that's gone or can't be decoded: tried again next run
            progress['failed'] += 1
        progress['done'] += 1
        if progress['done'] % SAVE_EVERY == 0:
            save_progress(progress_path, progress)
    # whatever was found is worth naming, even if this was stopped part way
    progress['state'] = 'naming'
    save_progress(progress_path, progress)
    progress['named'] = dog_model.name_new_dogs(connection, found)
    progress.update(state='stopped' if stopping else 'finished', finished=int(time.time()))
    save_progress(progress_path, progress)


if __name__ == '__main__':
    main(*sys.argv[1:])
