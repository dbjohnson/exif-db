import datetime
from subprocess import check_output

from api.exif import dataframe


def find_dupes():
    df = dataframe()
    # use the file size as initial screen for identical files
    counts = df['FileSize'].value_counts()
    df = df[df['FileSize'].isin(counts[counts > 1].index)]

    # now, check the MD5 of possible dupes
    df = df.assign(
        md5=df['SourceFile'].map(lambda p: check_output(["md5sum", p]).split()[0].decode())
    )
    counts = df['md5'].value_counts()
    return df[df['md5'].isin(counts[counts > 1].index)][[
        'SourceFile', 'md5', 'FileSize'
    ]]


def exif_date_to_datetime(exif_date):
    return datetime.date(*[
        int(v)
        for v in exif_date.split(' ')[0].split(':')
    ])


def migration_plan():
    df = dataframe()
    df = df[df['FileType'].isin(('CR2', 'ARW'))]
    df = df.assign(date=df['DateTimeOriginal'].map(exif_date_to_datetime))
    df = df.assign(
        destination=df.apply(
            lambda row: f"{row['date'].year}/{row['date'].month:02d}/{row['SourceFile'].split('/')[-1]}",
            axis=1
        )
    )
