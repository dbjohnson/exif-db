#!/bin/bash

EXIF_CSV="/storage/exif.csv"
TAGS_FILE=$(dirname "$(realpath "$0")")/tags.txt

if [ -f ${EXIF_CSV} ]; then
    find /photo -not -path '*/[@.]*' -type f -newer ${EXIF_CSV} > tmp.txt && \
        exiftool -@ "${TAGS_FILE}" -@ tmp.txt -fast -n -api "missingtagvalue^=" -csv \
        | tail -n +2 >> "${EXIF_CSV}" && rm tmp.txt
else
    exiftool -@ "${TAGS_FILE}" -csv -i @eaDir -fast -progress -n -r /photo > "${EXIF_CSV}"
fi
