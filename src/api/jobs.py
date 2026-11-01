"""
Long jobs that run as their own process, so that they survive a page being closed and can be
watched from any worker.
"""
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading

from api.progress import remaining_secs


SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts')


class Job:
    def __init__(self, name, script):
        self.worker = os.path.join(SCRIPTS_DIR, script)
        self.queue = f"/storage/{name}_queue.jsonl"
        self.progress_path = f"/storage/{name}_progress.json"
        self.lock = f"/storage/{name}.lock"

    def running(self):
        # the worker holds the lock for as long as it runs
        try:
            with open(self.lock) as fh:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except FileNotFoundError:
            return False
        except BlockingIOError:
            return True

    def progress(self):
        try:
            with open(self.progress_path) as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def status(self):
        is_running = self.running()
        progress = self.progress()
        # a worker that stopped without saying so, such as in a restart, still reads as running
        if progress and not is_running and progress['state'] == 'running':
            progress['state'] = 'interrupted'
        if progress and is_running and progress['state'] == 'running':
            progress['remaining_secs'] = remaining_secs(progress['started'], progress['done'], progress['total'])
        return {'running': is_running, 'progress': progress}

    def start(self, items, *args):
        """
        Work through the given items in the background.  The worker is given the queue, these
        arguments, and where to report progress and hold its lock.  Returns False if it's
        already running
        """
        if self.running():
            return False
        tmp = f"{self.queue}.{os.getpid()}"
        with open(tmp, 'w') as fh:
            for item in items:
                fh.write(json.dumps(item) + '\n')
        # whole, since with multiple workers another request can be starting this too
        os.replace(tmp, self.queue)
        proc = subprocess.Popen(
            [sys.executable, self.worker, self.queue, *args, self.progress_path, self.lock],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        # reap the process when it exits
        threading.Thread(target=proc.wait, daemon=True).start()
        return True

    def cancel(self):
        # returns False if there's nothing to stop
        progress = self.progress()
        if not self.running() or not progress:
            return False
        try:
            with open(f"/proc/{progress['pid']}/cmdline") as fh:
                if self.worker not in fh.read().split('\0'):
                    return False
            os.kill(progress['pid'], signal.SIGTERM)
            return True
        except (FileNotFoundError, ProcessLookupError):
            return False
