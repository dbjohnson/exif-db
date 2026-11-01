"""
Sends photos with faces in them to Amazon Rekognition, and notes which faces it finds and which of
them look like each other.  Meant for a container of its own, since it's the only thing that needs
the AWS key.  Photos come from the running app, so this sees what the library sees: no hidden or
deleted photos, and display copies rather than originals.

Each call costs about $0.001, so this stops at a limit on calls.  It carries on from its own logs if
run again, without paying twice.  Create a file called STOP in its folder to stop it between photos.

Usage: index_faces.py --year 2025 [--max-calls 8000]
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

import boto3
from botocore.config import Config

DIR = os.getenv('REKOGNITION_DIR', '/storage/rekognition')
APP = os.getenv('EXIF_DB_URL', 'http://localhost:8668')
REGION = os.getenv('AWS_REGION', 'us-east-1')
COLLECTION = 'exif-db'
INDEXED = f'{DIR}/indexed.jsonl'
SEARCHED = f'{DIR}/searched.jsonl'
STATUS = f'{DIR}/status.json'
STOP = f'{DIR}/STOP'
MATCH_THRESHOLD = 90
MAX_MATCHES = 200
MAX_FACES_PER_PHOTO = 20
# Rekognition's limit for an image sent directly
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# past what the check for faces has to reach, since it also goes through hidden photos
QUEUE_MARGIN = 100
WAIT_SECS = 30


def log(message):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), message, flush=True)


def get(path):
    with urllib.request.urlopen(APP + path, timeout=120) as response:
        return response.read()


def month_photos(year, month, with_faces):
    query = '?with_faces=true' if with_faces else ''
    html = get(f'/library/{year}/{month:02d}{query}').decode()
    # a month with nothing to show goes to the library page, which lists no photos
    found = re.search(r'const PHOTOS = (.*);\n', html)
    return json.loads(found[1]) if found else []


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh]


def append_jsonl(path, row):
    with open(path, 'a') as fh:
        fh.write(json.dumps(row) + '\n')


def save_status(**status):
    tmp = f'{STATUS}.tmp'
    with open(tmp, 'w') as fh:
        json.dump({**status, 'updated': int(time.time())}, fh)
    os.replace(tmp, STATUS)


def wait_for_face_check(year):
    # it works newest first, so it has to have got through everything from this year onwards
    needed = QUEUE_MARGIN + sum(
        len(month_photos(y, month, False))
        for y in range(year, time.localtime().tm_year + 1)
        for month in range(1, 13)
    )
    log(f'waiting for the check for faces to reach {needed:,} photos')
    while not os.path.exists(STOP):
        checked = json.loads(get('/api/faces'))['checked']
        save_status(state='waiting for the check for faces', checked=checked, needed=needed)
        if checked >= needed:
            log(f'the check for faces has reached {checked:,}')
            return
        time.sleep(WAIT_SECS)


def describe_face(record):
    return {
        'face_id': record['Face']['FaceId'],
        'box': record['Face']['BoundingBox'],
        'confidence': record['Face']['Confidence'],
        'quality': record['FaceDetail'].get('Quality')
    }


def run(year, max_calls):
    wait_for_face_check(year)
    client = boto3.client(
        'rekognition',
        region_name=REGION,
        config=Config(retries={'mode': 'adaptive', 'max_attempts': 10}, read_timeout=60)
    )
    if COLLECTION not in client.list_collections(MaxResults=100)['CollectionIds']:
        client.create_collection(CollectionId=COLLECTION)
        log(f'created collection {COLLECTION}')

    photos = [photo for month in range(1, 13) for photo in month_photos(year, month, True)]
    indexed = read_jsonl(INDEXED)
    searched = read_jsonl(SEARCHED)
    done = {row['id'] for row in indexed}
    # every run's calls count towards the limit, so running again can't quietly spend more
    calls = len(indexed) + len(searched)
    log(f'{len(photos):,} photos with faces in {year}, {len(done):,} already sent, {calls:,} calls so far')

    def progress(state):
        save_status(
            state=state,
            year=year,
            photos=len(photos),
            indexed=len(done),
            faces=sum(len(row['faces']) for row in indexed),
            searched=len(searched),
            calls=calls,
            max_calls=max_calls
        )

    def should_stop():
        if os.path.exists(STOP):
            progress('stopped')
            log('stopped by request')
            return True
        if calls >= max_calls:
            progress('reached the limit on calls')
            log(f'reached the limit of {max_calls:,} calls')
            return True
        return False

    for photo in photos:
        if photo['id'] in done:
            continue
        if should_stop():
            return
        rotate = f"?rotate={photo['rotation']}" if photo['rotation'] else ''
        try:
            image = get(f"/api/image{urllib.parse.quote(photo['path'])}{rotate}")
        except Exception as error:
            log(f"no image for {photo['path']}: {error}")
            continue
        if len(image) > MAX_IMAGE_BYTES:
            log(f"too large to send: {photo['path']}")
            continue
        calls += 1
        response = client.index_faces(
            CollectionId=COLLECTION,
            Image={'Bytes': image},
            ExternalImageId=hashlib.sha1(photo['id'].encode('utf-8', 'surrogateescape')).hexdigest(),
            MaxFaces=MAX_FACES_PER_PHOTO,
            QualityFilter='AUTO',
            DetectionAttributes=['DEFAULT']
        )
        row = {
            'id': photo['id'],
            'path': photo['path'],
            'rotation': photo['rotation'],
            'faces': [describe_face(record) for record in response['FaceRecords']],
            'unindexed': len(response.get('UnindexedFaces', []))
        }
        append_jsonl(INDEXED, row)
        indexed.append(row)
        done.add(photo['id'])
        if len(done) % 50 == 0:
            progress('sending photos')
            log(f"sent {len(done):,} of {len(photos):,}, {sum(len(r['faces']) for r in indexed):,} faces, {calls:,} calls")

    matched = {row['face_id'] for row in searched}
    faces = [face['face_id'] for row in indexed for face in row['faces'] if face['face_id'] not in matched]
    log(f'{len(faces):,} faces to compare')
    for position, face_id in enumerate(faces, 1):
        if should_stop():
            return
        calls += 1
        response = client.search_faces(
            CollectionId=COLLECTION,
            FaceId=face_id,
            FaceMatchThreshold=MATCH_THRESHOLD,
            MaxFaces=MAX_MATCHES
        )
        row = {
            'face_id': face_id,
            'matches': [
                {'face_id': match['Face']['FaceId'], 'similarity': match['Similarity']}
                for match in response['FaceMatches']
            ]
        }
        append_jsonl(SEARCHED, row)
        searched.append(row)
        if position % 100 == 0:
            progress('comparing faces')
            log(f'compared {position:,} of {len(faces):,}, {calls:,} calls')
    progress('finished')
    log(f'finished after {calls:,} calls')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--year', type=int, required=True)
    parser.add_argument('--max-calls', type=int, default=8000)
    args = parser.parse_args()
    try:
        run(args.year, args.max_calls)
    except Exception as error:
        log(f'failed: {error!r}')
        save_status(state='failed', error=repr(error))
        sys.exit(1)
