# exif-db
Exif database and API

This codebase contains scripts to collect exif data and expose via database and API interfaces.  It's a light wrapper around the excellent [exiftool](https://exiftool.org/), which does all the heavy lifting.

## Usage

Modify [compose.yml](./src/compose.yml) to mount your media directory and a volume for storage


Scan all files for exif data; subsequent runs will only check files added or changed since the start of the last successful scan
```bash
docker compose run --rm -d exif-db bash /src/app/scripts/scan.sh
```

To rebuild the index from scratch, add `--full`.  The current index stays in use until the rebuild succeeds, and is then kept as `exif.csv.bak`
```bash
docker compose run --rm -d exif-db bash /src/app/scripts/scan.sh --full
```

Scans can also be started, cancelled and scheduled from the webservice at `/manage`, including the time of day for daily scans (in the container's `TZ`).

Start webservice / API
```bash
docker compose up -d
```

## Reviewing photos

Only approved photos appear in the gallery and kiosk.  New photos start unreviewed: a date with some shows a link to review them, and `/manage` links to everything awaiting review.  Photos left unchecked in the review grid are hidden, not deleted.  Each date also links to an editor for changing the approvals already made there, and approvals can be reset to unreviewed for a date or, from `/manage`, for the whole library.

The review queue shows photos taken within 5 seconds of each other on one camera as a single tile (up to 12), with a button to open it up.  Only that tile's photo starts checked.  Photos with no camera make or model, like screenshots, also start unchecked.

Decisions are stored in `reviews.csv` in the storage volume, and survive a full rebuild.  Review thumbnails are cached in `thumbs/` in the storage volume, which is safe to delete.

Approved photos can be favorited from the gallery or kiosk (stored in `favorites.csv`).  A gallery date with no approved photos uses a random favorite as its background, or any approved photo if there are no favorites.  The kiosk fills in with favorites and then other approved photos when too few are from around today.

Photos can be rotated from the gallery or kiosk (stored in `rotations.csv`).  Originals are never changed: a rotated copy is cached in `thumbs/` for display.

Deleting moves a photo to `deleted/` in the storage volume and records it in `deletions.csv`, and can be undone straight afterwards.  `/manage` shows how much is in the trash and can empty it.

## The library

`/library` browses the whole index by year and month, newest first, then shows a month's photos by day, also newest first, with a viewer for favoriting, rotating, hiding, deleting and downloading.  Unlike the gallery it includes photos that haven't been reviewed, since that's most of a library, but not hidden ones.  An unreviewed photo can be approved from the viewer, and favoriting one approves it too.  Photos with no date or an impossible one, such as from a camera whose clock was never set, are under Undated, by folder.

Any month, and Undated, has a Select button for approving, favoriting, rotating, hiding, deleting or setting the date of many photos at once (shift-click selects a range).  Undated also offers the date a folder's name suggests, such as `2002.03.11 - Escalante` or `2024/04`.  Dates set this way are stored in `dates.csv`, apply everywhere a photo's date is used, and survive a full rebuild.  Original files aren't changed.

## Finding faces

`/manage` can check the library for photos with faces in them, which adds a filter to `/library`.  It runs on the server, using [YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) on each photo's thumbnail, and no photos leave it.  It takes about 2 hours per 60,000 photos on a Celeron J4125.  It only notes that a face is there, not whose it is.

It also starts by itself after each successful scan, for whatever hasn't been checked yet, such as photos the scan just added.  
Results are kept in `faces.csv` in the storage volume, so stopping or restarting loses at most a few photos' work and starting again carries on.

## People

`scripts/find_people.py` looks at photos the check above found faces in, and describes each face with [InsightFace's](https://github.com/deepinsight/insightface) `buffalo_l` models, using the same display copy the app shows.  It runs on the server after each check for faces finds new photos, a few seconds per photo on a Celeron J4125, and nothing leaves the server.  The models are for non-commercial use.

The first time through a library takes days at that speed, so it can be done from a faster computer.  `scripts/scan_people_remote.py` asks the running app for its display copies and writes a `people.db`, which is safe to stop and run again, then `scripts/cluster_people.py` works out which faces look alike:

```bash
python scripts/fetch_face_models.py models
python scripts/scan_people_remote.py http://your-server:8668 people.db models
python scripts/cluster_people.py people.db
```

Copy `people.db` into the storage volume before starting the app with it.  Faces, names and covers are all kept in that file.  Faces too small or too poor to compare are kept but not offered for naming, as they're mostly blurs and the backs of heads.

`/people` shows groups of faces that look alike, largest first.  Naming a group, or some of the faces in it, adds that person's photos to their own page, one year at a time.

Edit on a person's page renames them, or removes them, which sends their faces back to "Who is this?" and leaves the photos alone.  Using a name someone already has joins the two together.  All of these can be undone straight afterwards.

The viewer's person button draws a box around each face found in a photo, and a person's page starts out showing them.  Pressing a box or a face in the row below it gives that face a different name, takes its name off, skips it, or makes it the person's cover.  A person's Faces page also shows any face in its photo, and selecting one there can make it their cover.  A person's cover is otherwise their largest, clearest face.

## Places

`/places` finds photos and movies by where they were taken: a town, state or country, by what they recorded themselves, and any folder by its name, like `2002.03.11 - Escalante`.  Many phones and old cameras don't record a location, so folder names cover a lot of older photos.

`scripts/index_locations.py` reads locations into `locations.csv` in the storage volume at the end of each scan, only for files it hasn't seen, and works out the nearest town from a list of [GeoNames](https://www.geonames.org/) towns of 1,000 or more people (CC BY 4.0).  Nothing is sent anywhere, unless you press the map link on a photo's info card, which opens OpenStreetMap.

## Info cards

The info button on a photo, or `i` in the viewer, shows who's in it, where it was taken, its camera, exposure and file details, and every tag in the index.  In the library, a mouse also gets favorite, rotate, hide, delete and info buttons on hovering over a thumbnail.

## Home movies

`/movies` plays every home movie in the order it was filmed, as one continuous stream: each movie runs straight into the next, across months and years.  The years, months and movies are listed newest first, like the library, while the stream itself always plays forward in time.  Pick a year and month to drop in at the start of that month, or a movie in the month's list, and the scrubber covers the month being played.  It picks up where it left off on that device.

Keyboard: Space plays and pauses, ←/→ moves 10 seconds and carries over into the next or previous movie, Shift+←/→ moves by movie, ↑/↓ moves by month, F is full screen and M mutes.

Safari shows an AirPlay button when it can see a receiver.  The receiver fetches each movie itself, so it needs to reach the same address as the page: a `ts.net` name only resolves on the tailnet, so an Apple TV needs Tailscale installed or a local DNS entry for that name.

Movies are read by the end of each scan into `movies.csv` in the storage volume, which is safe to delete: the next scan rebuilds it, though reading every video takes a while (about 20 minutes for 11,000 on a Synology DS224+).  After that a scan only reads new ones.  Live Photos' motion clips, the same clip filed in several folders, and anything in `videos/Work movies` are left out.  Where a movie's name starts with a date, like `2009.10 Lucy's Birth.mp4`, or its folder's does, that says when it was filmed and is shown as its title, since an edited movie's own date is when it was exported.

Hiding takes a movie out of the stream (stored in `movies_hidden.csv`) and can be undone from the list at the bottom of the page.  Deleting takes two presses and moves the movie and any other copies of it to `deleted/` in the storage volume, with the same undo and `/manage` trash as photos.  A same-named photo is left alone.  A large movie can take minutes to reach the trash, as it's copied there, and only then can the delete be undone.

Browsers get the original where they can play it, which is any H.264 and also HEVC if the browser has it.  Otherwise they get a copy Synology made in `@eaDir`, such as for old MPEG-4 and AVI files, and nothing is converted here.  A movie with neither is skipped with a message.

## Duplicates

After a scan changes the index, the server looks for photos that are copies of each other, such as a screenshot of a photo that was then texted around.  Copies it's sure about are hidden automatically, keeping the best one: the most detail first, then one from a camera, one that isn't a screenshot, and one with a date.  The copy that stays takes on any approval or favorite the others had, so nothing drops out of the gallery or kiosk.  Nothing is deleted.

`/duplicates` shows what was hidden, where another copy can be kept instead or a hidden one brought back, which sticks through later checks.  Photos that may be copies but aren't certain, like annotated or brighter versions, wait there for a decision.  Shots from a camera's burst or timer are never treated as copies, and a copy is only hidden if it matches the one that's kept.

Each photo's fingerprint comes from its thumbnail, and only photos with similar fingerprints get compared, by lining them up and comparing them block by block.  Results are kept in `duplicate_fingerprints.csv` and `duplicate_pairs.csv` in the storage volume, so later checks only do what's new.  What's been dealt with is in `duplicates.csv`.  The first check takes a little over an hour per 60,000 photos on a Celeron J4125.

## The kiosk

`/kiosk` is meant to run unattended for days.  It keeps showing photos through server outages and picks up again by itself, updates when photos change or a new version is deployed, and reloads at midnight.  Once it has loaded over HTTPS, a service worker turns any failed page load in that browser into a screen that keeps retrying, including after a restart where the frame comes up before the server does.

## Images

Pages show copies sized for the screen, and downloads give the original file.  On a Synology, the copies Synology has already made in `@eaDir` are used where they're up to date.  Anything else is converted once, limited to `MAX_IMAGE_SIZE`, and cached in `thumbs/`.
