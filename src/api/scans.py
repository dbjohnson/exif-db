import datetime
import fcntl
import json
import os
import re
import signal
import subprocess
import threading
import time


SCAN_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'scripts',
    'scan.sh'
)
LOCK = '/storage/scan.lock'
LOG = '/storage/scan.log'
RESULT = '/storage/scan_result.json'
LAST_SCAN = '/storage/last_scan'
SCHEDULE = '/storage/scan_schedule.json'
SCHEDULER_POLL_SECS = 60
DAILY_HOURS = 24
DEFAULT_DAILY_TIME = '03:00'


def running():
    # scan.sh holds the lock for as long as it runs, including scans started outside the app
    try:
        with open(LOCK) as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except FileNotFoundError:
        return False
    except BlockingIOError:
        return True


def _read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _current():
    # the lock file holds "<pid> <mode> <start time>", written by scan.sh
    try:
        with open(LOCK) as fh:
            pid, mode, started = fh.read().split()
        return {'pid': int(pid), 'mode': mode, 'started': int(started)}
    except (FileNotFoundError, ValueError):
        return None


def log_tail(lines=15):
    try:
        with open(LOG, 'rb') as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 8192))
            return fh.read().decode(errors='replace').splitlines()[-lines:]
    except FileNotFoundError:
        return []


def _progress(log):
    # exiftool logs each file as "======== <path> [<n>/<total>]"
    for line in reversed(log):
        match = re.search(r'\[(\d+)/(\d+)\]$', line)
        if match:
            return {'done': int(match[1]), 'total': int(match[2])}
    return None


def status():
    is_running = running()
    log = log_tail()
    return {
        'running': is_running,
        'current': _current() if is_running else None,
        'progress': _progress(log) if is_running else None,
        'last_result': _read_json(RESULT),
        'schedule_hours': schedule_hours(),
        'schedule_time': schedule_time(),
        'next_scan': next_scan(),
        'log': log
    }


def start(full=False):
    """
    Start a scan in the background.  Returns False if one is already running
    """
    if running():
        return False
    proc = subprocess.Popen(
        ['bash', SCAN_SCRIPT] + (['--full'] if full else []),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    # reap the process when it exits
    threading.Thread(target=proc.wait, daemon=True).start()
    return True


def cancel():
    """
    Stop the running scan.  Returns False if there's nothing this app can stop: a scan
    run from another container has a pid that means something different here
    """
    current = _current()
    if not running() or current is None:
        return False
    try:
        with open(f"/proc/{current['pid']}/cmdline") as fh:
            if SCAN_SCRIPT not in fh.read().split('\0'):
                return False
        os.killpg(current['pid'], signal.SIGTERM)
        return True
    except (FileNotFoundError, ProcessLookupError):
        return False


def schedule_hours():
    return (_read_json(SCHEDULE) or {}).get('hours', 0)


def schedule_time():
    return (_read_json(SCHEDULE) or {}).get('at', DEFAULT_DAILY_TIME)


def set_schedule(hours, at=None):
    schedule = {'hours': hours, 'at': at or schedule_time()}
    with open(SCHEDULE, 'w') as fh:
        json.dump(schedule, fh)


def _next_daily(after, at):
    """
    The first time after the given timestamp that the clock shows "HH:MM", local time
    """
    hour, minute = map(int, at.split(':'))
    day = datetime.datetime.fromtimestamp(after).date()
    while True:
        candidate = datetime.datetime.combine(day, datetime.time(hour, minute)).timestamp()
        if candidate > after:
            return candidate
        day += datetime.timedelta(days=1)


def next_scan():
    hours = schedule_hours()
    if not hours:
        return None
    # count from attempts as well as successes, so a failing scan doesn't retry every poll
    started = (_read_json(RESULT) or {}).get('started', 0)
    last_success = os.path.getmtime(LAST_SCAN) if os.path.exists(LAST_SCAN) else 0
    last = max(started, last_success)
    if hours == DAILY_HOURS:
        # also count from when the schedule was set, so that choosing a time that has already
        # passed today waits for tomorrow.  A time missed while the NAS was off still runs
        # once it's back, since the last scan is then older than that time
        return _next_daily(max(last, os.path.getmtime(SCHEDULE)), schedule_time())
    return last + hours * 3600


def _run_scheduler():
    while True:
        due = next_scan()
        if due is not None and time.time() >= due:
            # with multiple workers, the scan lock lets only one of these through
            start()
        time.sleep(SCHEDULER_POLL_SECS)


def start_scheduler():
    threading.Thread(target=_run_scheduler, daemon=True).start()
