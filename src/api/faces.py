import json

from api.jobs import Job
from api.worklog import save_json


job = Job('faces', 'find_faces.py')
# which scan's new photos have been dealt with, by when it finished
LAST_SCAN_CHECKED = '/storage/faces_last_scan.json'


def last_scan_checked():
    try:
        with open(LAST_SCAN_CHECKED) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return 0


def set_last_scan_checked(finished):
    save_json(LAST_SCAN_CHECKED, finished)
