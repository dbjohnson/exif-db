import fcntl
import json
import os
import signal
import subprocess
import sys
import threading


WORKER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'scripts',
    'find_faces.py'
)
QUEUE = '/storage/faces_queue.jsonl'
PROGRESS = '/storage/faces_progress.json'
LOCK = '/storage/faces.lock'


def running():
    # the worker holds the lock for as long as it runs
    try:
        with open(LOCK) as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except FileNotFoundError:
        return False
    except BlockingIOError:
        return True


def _progress():
    try:
        with open(PROGRESS) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def status():
    is_running = running()
    progress = _progress()
    # a worker that stopped without saying so, such as in a restart, still reads as running
    if progress and not is_running and progress['state'] == 'running':
        progress['state'] = 'interrupted'
    return {'running': is_running, 'progress': progress}


def start(photos, results):
    """
    Check the given photos in the background, saving to the given log.  Returns False if a
    check is already running
    """
    if running():
        return False
    tmp = f"{QUEUE}.{os.getpid()}"
    with open(tmp, 'w') as fh:
        for photo in photos:
            fh.write(json.dumps(photo) + '\n')
    # whole, since with multiple workers another request can be starting this too
    os.replace(tmp, QUEUE)
    proc = subprocess.Popen(
        [sys.executable, WORKER, QUEUE, results, PROGRESS, LOCK],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    # reap the process when it exits
    threading.Thread(target=proc.wait, daemon=True).start()
    return True


def cancel():
    # returns False if there's nothing to stop
    progress = _progress()
    if not running() or not progress:
        return False
    try:
        with open(f"/proc/{progress['pid']}/cmdline") as fh:
            if WORKER not in fh.read().split('\0'):
                return False
        os.kill(progress['pid'], signal.SIGTERM)
        return True
    except (FileNotFoundError, ProcessLookupError):
        return False
