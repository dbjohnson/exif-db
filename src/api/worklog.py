"""
What long jobs' own processes share with the web app, so this can't import the rest of the api
package: importing that would start a web app.
"""
import csv
import fcntl
import json
import os
import time


def append_log(path, rows):
    # "<key>,<value>,<time>", where a log's latest row for a key wins; see exif.py
    with open(path, 'a', newline='', encoding='utf-8', errors='surrogateescape') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        # taken under the lock, so timestamps follow the order rows were written
        now = time.time_ns()
        writer = csv.writer(fh, lineterminator='\n')
        writer.writerows((key, value, now) for key, value in rows)


def save_json(path, value):
    # whole, since a page can be reading this at any moment
    tmp = f"{path}.{os.getpid()}"
    with open(tmp, 'w') as fh:
        json.dump(value, fh)
    os.replace(tmp, path)


def hold_lock(path):
    """
    Returns a lock that's held until this process ends however it ends, or None if another has it
    """
    lock = open(path, 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock
    except BlockingIOError:
        lock.close()
        return None
