#!/bin/bash

EXIF_CSV="/storage/exif.csv"

if [ -f ${EXIF_CSV} ]; then
    # Make sure we extract the same tags as before
    # Extract the header row from the existing CSV file
    HEADER=$(head -n 1 "$EXIF_CSV")

    # Remove any double quotes from the header
    HEADER_CLEAN=${HEADER//\"/}

    IFS=',' read -ra TAGS <<< "$HEADER_CLEAN"

    # Create or empty the taglist.txt file
    TAGLIST_FILE="taglist.txt"
    > "$TAGLIST_FILE"

    # Loop through the tag names and write them to taglist.txt
    for TAG in "${TAGS[@]}"; do
        if [ "$TAG" != "SourceFile" ]; then
            echo "-$TAG" >> "$TAGLIST_FILE"
        fi
    done
    find /photo -newer ${EXIF_CSV} -exec \
        exiftool -@ taglist.txt -f -api "missingtagvalue^=" -csv {} \; \
        | tail -n +2 >> "${EXIF_CSV}"
else
    exiftool -MIMEType -all -csv -i @eaDir -fast -r /photo > "${EXIF_CSV}"
fi
