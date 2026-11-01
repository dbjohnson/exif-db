#!/usr/bin/env python3
"""
Keeps movies.csv up to date with the videos in exif.csv.  Run at the end of each scan.

Movies need tags the photo index doesn't collect, like their length, and adding those to
tags.txt would misalign rows appended to an existing index until a full rebuild.  So they
have their own file, where only videos not seen before need reading.
"""
import csv
import os
import subprocess
import tempfile


EXIF_CSV = '/storage/exif.csv'
MOVIES_CSV = '/storage/movies.csv'
TAGS = ('Duration', 'CreationDate', 'CreateDate', 'DateTimeOriginal', 'CompressorID', 'ContentIdentifier')
FIELDS = ('SourceFile', 'FileSize', *TAGS, 'HasPhoto')


def read_rows(path):
    with open(path, newline='', encoding='utf-8', errors='surrogateescape') as fh:
        yield from csv.DictReader(fh)


def stem(name):
    return os.path.splitext(name)[0].lower()


def read_index():
    """
    The videos in the photo index, and the (folder, name) of its images.  A rescanned file
    appears again further down, so the last row for a path is the one that counts
    """
    videos = {}
    images = set()
    for row in read_rows(EXIF_CSV):
        mime = row.get('MIMEType') or ''
        path = row.get('SourceFile')
        if not path:
            continue
        if mime.startswith('video/'):
            videos[path] = row.get('FileSize') or ''
        elif mime.startswith('image/'):
            folder, name = os.path.split(path)
            images.add((folder, stem(name)))
    return videos, images


def probe(paths):
    # one that can't be read is still recorded, so that it isn't tried again by every scan
    results = {path: {} for path in paths}
    if not paths:
        return results
    with tempfile.NamedTemporaryFile('w', suffix='.txt', encoding='utf-8', errors='surrogateescape') as listing:
        listing.write('\n'.join(paths))
        listing.flush()
        with tempfile.TemporaryFile('w+', newline='', encoding='utf-8', errors='surrogateescape') as output:
            # exits non-zero if any single file is unreadable, so go by what it wrote
            subprocess.run(
                [
                    'exiftool', '-csv', '-n', '-fast', '-progress', '-api', 'missingtagvalue^=',
                    *(f"-{tag}" for tag in TAGS), '-@', listing.name
                ],
                stdout=output,
                check=False
            )
            output.seek(0)
            for row in csv.DictReader(output):
                if row.get('SourceFile') in results:
                    results[row['SourceFile']] = row
    return results


def main():
    videos, images = read_index()
    known = {}
    if os.path.exists(MOVIES_CSV):
        known = {row['SourceFile']: row for row in read_rows(MOVIES_CSV) if row.get('SourceFile')}
    # a file that has changed needs reading again
    changed = [path for path, size in videos.items() if path not in known or known[path].get('FileSize') != size]
    probed = probe(changed)

    tmp = f"{MOVIES_CSV}.tmp"
    with open(tmp, 'w', newline='', encoding='utf-8', errors='surrogateescape') as fh:
        writer = csv.DictWriter(fh, FIELDS, extrasaction='ignore')
        writer.writeheader()
        for path, size in videos.items():
            folder, name = os.path.split(path)
            writer.writerow({
                **known.get(path, {}),
                **probed.get(path, {}),
                'SourceFile': path,
                'FileSize': size,
                # worked out every time, since the photo can arrive after its video
                'HasPhoto': int((folder, stem(name)) in images)
            })
    os.replace(tmp, MOVIES_CSV)


if __name__ == '__main__':
    main()
