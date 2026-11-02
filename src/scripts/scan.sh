#!/bin/bash
# Usage: scan.sh [--full]
# Scans files changed since the last scan, or everything if --full is given, there is
# no index yet, or the tags to read have changed since the index was made.  A full scan
# replaces the index only once it has succeeded.

EXIF_CSV="/storage/exif.csv"
LAST_SCAN="/storage/last_scan"
OUTPUT="${EXIF_CSV}.scan"
LOCK="/storage/scan.lock"
LOG="/storage/scan.log"
RESULT="/storage/scan_result.json"
TAGS_FILE=$(dirname "$(realpath "$0")")/tags.txt
# defines tags of our own, such as Screenshot; exiftool needs -config before anything else
CONFIG_FILE=$(dirname "$(realpath "$0")")/exiftool.config
# what tags the index was made with: new ones need every file read again, as appending
# rows with other columns would put values under the wrong ones
TAGS_STAMP="/storage/scan_tags"
TAGS=$(cat "${TAGS_FILE}" "${CONFIG_FILE}" | md5sum | cut -d ' ' -f 1)

if [ "$1" == "--full" ] || [ ! -f ${EXIF_CSV} ] || [ "$(cat "${TAGS_STAMP}" 2>/dev/null)" != "${TAGS}" ]; then
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
        exiftool -config "${CONFIG_FILE}" -f -@ "${TAGS_FILE}" -@ tmp.txt -fast -progress -n -api "missingtagvalue^=" -csv > "${OUTPUT}"
        [ -s "${OUTPUT}" ] || exit 1
        tail -n +2 "${OUTPUT}" >> "${EXIF_CSV}"
    else
        echo "No new files" >&2
    fi
else
    exiftool -config "${CONFIG_FILE}" -f -@ "${TAGS_FILE}" -csv -i @eaDir -fast -progress -n -api "missingtagvalue^=" -r /photo > "${OUTPUT}"
    [ -s "${OUTPUT}" ] || exit 1
    [ -f "${EXIF_CSV}" ] && cp -p "${EXIF_CSV}" "${EXIF_CSV}.bak"
    mv "${OUTPUT}" "${EXIF_CSV}"
    echo "${TAGS}" > "${TAGS_STAMP}"
fi

mv "${LAST_SCAN}.tmp" "${LAST_SCAN}"

# the photo index is good by this point, so trouble with movies doesn't fail the scan
python3 "$(dirname "$(realpath "$0")")/index_movies.py" || echo "Couldn't index movies" >&2
python3 "$(dirname "$(realpath "$0")")/index_locations.py" || echo "Couldn't index locations" >&2
