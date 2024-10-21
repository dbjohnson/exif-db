#!/bin/bash

EXIF_CSV="/storage/exif.csv"
TAGS_FILE=$(dirname "$(realpath "$0")")/tags.txt

if [ -f ${EXIF_CSV} ]; then
    find /photo -newer ${EXIF_CSV} -exec \
        exiftool -@ "${TAGS_FILE}" -fast -n -api "missingtagvalue^=" -csv {} \; \
        | tail -n +2 >> "${EXIF_CSV}"
else
    exiftool -@ "${TAGS_FILE}" -csv -i @eaDir -fast -progress -n -r /photo > "${EXIF_CSV}"
fi
