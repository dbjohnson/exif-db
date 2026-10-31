#!/bin/bash
# Usage: scan.sh [--full]
# Scans files changed since the last scan, or everything if --full is given (or there
# is no index yet).  A full scan replaces the index only once it has succeeded.

EXIF_CSV="/storage/exif.csv"
LAST_SCAN="/storage/last_scan"
OUTPUT="${EXIF_CSV}.scan"
LOCK="/storage/scan.lock"
LOG="/storage/scan.log"
RESULT="/storage/scan_result.json"
TAGS_FILE=$(dirname "$(realpath "$0")")/tags.txt

if [ "$1" == "--full" ] || [ ! -f ${EXIF_CSV} ]; then
    MODE="full"
else
    MODE="incremental"
fi

# only one scan at a time; the lock is held until this process and its children exit
exec 9>>"${LOCK}"
flock -n 9 || { echo "A scan is already running" >&2; exit 1; }

STARTED=$(date +%s)
echo "$$ ${MODE} ${STARTED}" > "${LOCK}"
exec 2>"${LOG}"

finish() {
    local code=$?
    rm -f "${OUTPUT}" "${LAST_SCAN}.tmp" tmp.txt
    echo "{\"mode\": \"${MODE}\", \"started\": ${STARTED}, \"finished\": $(date +%s), \"exit_code\": ${code}}" > "${RESULT}"
}
trap finish EXIT
trap 'exit 143' TERM INT

# note the start time, so that files added during this scan are picked up by the next
touch "${LAST_SCAN}.tmp"

# exiftool exits non-zero if any single file is unreadable, but only writes its CSV
# once it has finished - so check for output rather than checking the exit status
if [ "${MODE}" == "incremental" ]; then
    # exif.csv's modified time is only a fallback for libraries not yet scanned with
    # a last_scan file
    [ -f "${LAST_SCAN}" ] && NEWER="${LAST_SCAN}" || NEWER="${EXIF_CSV}"
    # by when files arrived rather than their modified times: phone backups keep a photo's
    # original modified time, so most of what they add looks older than the last scan
    find /photo -not -path '*/[@.]*' -type f -cnewer "${NEWER}" > tmp.txt || exit 1
    if [ -s tmp.txt ]; then
        exiftool -f -@ "${TAGS_FILE}" -@ tmp.txt -fast -progress -n -api "missingtagvalue^=" -csv > "${OUTPUT}"
        [ -s "${OUTPUT}" ] || exit 1
        tail -n +2 "${OUTPUT}" >> "${EXIF_CSV}"
    else
        echo "No new files" >&2
    fi
else
    exiftool -f -@ "${TAGS_FILE}" -csv -i @eaDir -fast -progress -n -api "missingtagvalue^=" -r /photo > "${OUTPUT}"
    [ -s "${OUTPUT}" ] || exit 1
    [ -f "${EXIF_CSV}" ] && cp -p "${EXIF_CSV}" "${EXIF_CSV}.bak"
    mv "${OUTPUT}" "${EXIF_CSV}"
fi

mv "${LAST_SCAN}.tmp" "${LAST_SCAN}"
