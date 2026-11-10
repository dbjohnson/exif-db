# exif-db
Exif database and API

This codebase contains scripts to collect exif data and expose via database and API interfaces.  It's a light wrapper around the excellent [exiftool](https://exiftool.org/), which does all the heavy lifting.

## Usage

Modify [compose.yml](./src/compose.yml) to mount your media directory and a volume for storage


Scan all files for exif data; subsequent runs will only check files added or changed since the start of the last successful scan
```bash
docker compose run --rm -d exif-db bash /src/app/scripts/scan.sh
```

To rebuild the index from scratch, add `--full`.  The current index stays in use until the rebuild succeeds, and is then kept as `exif.csv.bak`.  A scan also rebuilds it by itself after `scripts/tags.txt` or `scripts/exiftool.config` changes, since every file has to be read again for the new tags (about 2 hours for 80,000 files on a Synology DS224+, measured in November 2026)
```bash
docker compose run --rm -d exif-db bash /src/app/scripts/scan.sh --full
```

Scans can also be started, cancelled and scheduled from the webservice at `/manage`, including the time of day for daily scans (in the container's `TZ`).

Start webservice / API
```bash
docker compose up -d
```

## Reviewing photos

Approving decides what the kiosk shows, along with recent photos that haven't been reviewed yet (see [The kiosk](#the-kiosk)).  New photos start unreviewed, and `/manage` links to everything awaiting review.  Photos left unchecked in the review grid are hidden, not deleted.  Each date also links to an editor for changing the approvals already made there, and approvals can be reset to unreviewed for a date or, from `/manage`, for the whole library.

The review queue shows photos taken within 5 seconds of each other on one camera as a single tile (up to 12) if each one also looks like the one before it, with a button to open it up.  Only that tile's photo starts checked.  How alike they look comes from the duplicate check's fingerprints, so a new photo has its own tile until that check has run.  Photos with no camera make or model, like screenshots, also start unchecked.

Decisions are stored in `reviews.csv` in the storage volume, and survive a full rebuild.  Review thumbnails are cached in `thumbs/` in the storage volume, which is safe to delete.

Photos can be favorited from the library or kiosk (stored in `favorites.csv`).  The kiosk fills in with favorites and then other approved photos when too few are from around today.

Photos can be rotated from the library or kiosk (stored in `rotations.csv`).  Originals are never changed: a rotated copy is cached in `thumbs/` for display.

Photos can also be enhanced from the library, with the magic wand in the viewer (or `e`) or in the selection bar (stored in `enhanced.csv`).  An enhanced photo is shown brighter and more colorful everywhere it appears: thumbnails, the viewer, its raw file, the review queue, the kiosk and shared links.  The duplicates page shows photos as their files are, for comparing copies.  The curve is worked out from each photo (`enhance` in `api/thumbs.py`): it stretches the tones so a few pixels reach black and white, lifts the shadows more the more of the photo is dark, lightens a photo that's still dark overall, and raises saturation by 20%.  As with rotating, the file isn't changed: the enhanced copy is cached in `thumbs/` next to the plain one, and pressing the button again shows the photo as taken.

Deleting moves a photo to `deleted/` in the storage volume and records it in `deletions.csv`, and can be undone straight afterwards, or later from the library's Recycle bin.  `/manage` shows how much is in the trash and can empty it.

## Photos

`/library`, Photos in the navigation bar, shows every photo, newest first, as one long list by day, each day's heading with its year, with a viewer for favoriting, rotating, hiding, deleting and downloading.  It includes photos that haven't been reviewed, since that's most of a library, but not hidden ones.  An unreviewed photo can be approved from the viewer, and favoriting one approves it too.  Photos with no date or an impossible one, such as from a camera whose clock was never set, come last, under Undated, by folder.

Every month takes its full height from the start and fills in as it comes near the screen, so the year and month boxes at the top go straight to a date (Undated is the month box's last choice), and scrolling either way from there carries on as usual.  They follow along while scrolling, and so does the address (`/library?date=2016-07`), so a reload stays in the same place.  With a photo open, the address names it (`&open=` and its id), on any list of photos, so a reload or a copied address opens the same photo on the same list.  Back closes the photo and leaves the list's own address.  In the same row, the library can be narrowed to tags and people, from one list, and to a place, which can be a named place, a town, a state, a country or a folder.  Each tag or person added narrows it further: Jane and Lucy shows only photos with both.  A filter in use shows as its name with an × that takes just that one off.  On a phone the filters and buttons in that row are icons, so it fits across the screen.  Folders, at the end of the row, has Favorites, Hidden, the Recycle bin, Cameras, Screenshots and each tag.

On a screen at least 1,024 pixels wide, the dates and filters are down the side instead: every year, the months of the one showing with Undated under them, the tags and people chosen with the list for adding another, the place, and the folders.  What's left of the row, such as Select, sits on the navigation bar's line, so the header is one line.  Videos have the same sidebar, with tags only.

The viewer's share button gives a link to the photo, `/photo?id=…`, through the device's share sheet where there is one or else copied.  It opens the photo in the viewer wherever it is in the library, even after its date has changed, or in Hidden if it's been hidden.  The video player's share button does the same for a video, as `/movie?path=…`, which opens the player on it.  Links work for anyone who can reach the app.

Both links first give a short page with Open Graph tags, which Messages and other apps build a link's preview from: a photo's date and place with the photo itself, and a video's title, date, place and length with its poster and the video (an H.264 copy where there is one).  Apps fetching a preview get the page without its script, and a browser goes straight on to the photo or video.  Whole addresses in the tags come from the `X-Forwarded-Host` and `X-Forwarded-Proto` headers, or else the `Host` header, with https for a `ts.net` name.

A person's, tag's, place's or camera's photos, and Favorites, Hidden and Screenshots, are the same list narrowed to them, with the same date boxes.  The navigation bar and that one row stay at the top of every page while scrolling.

Select, then photos one by one or Select all in the bar, picks many photos at once, for approving, favoriting, rotating, enhancing, hiding, deleting, tagging, or setting their date or place (shift-click selects a range).  A list of up to 3,000 can be selected all at once.  Undated also offers the date a folder's name suggests, such as `2002.03.11 - Escalante` or `2024/04`, for each folder, or for every folder that has one at once from the button at the top of Undated, which can be undone.  Dates set this way are stored in `dates.csv`, apply everywhere a photo's date is used, and survive a full rebuild.  Original files aren't changed.

## Folders

`/library/folders`, linked from the top of the library, has folders that gather photos from every month, each the same list as the library narrowed to them.  They show even when empty.

- `/onthisday` has the photos taken on today's date in every year, newest year first, as days of the list.  Arrows beside the date go to the day before or after, and the calendar button comes back to today.  The address stays `/onthisday` throughout, without the date that other lists add as they're scrolled, so a bookmark of it always opens today.  `?day=MM-DD` opens any day, and is taken off the address once it has.  With a photo open, the address names its day and the photo, and goes back to `/onthisday` when the photo is closed.  It has no button in the navigation bar: it's the first folder, and in the sidebar.
- `/library/favorites` has every favorite that isn't hidden.  Unfavoriting a photo there takes it out, with an undo.
- `/library/hidden` has photos hidden from the library or left unchecked in the review queue.  Unhide puts a photo back in the library as unreviewed.  Copies hidden automatically as duplicates aren't here; `/duplicates` has those.
- `/library/recycle-bin` has photos deleted from the library, by the day they were deleted (from `deletions.csv`, or for older deletions the time the file moved).  Restore moves a photo's files back; one deleted before the last scan returns to the library after the next one.  Delete forever removes its files from the NAS for good, along with the thumbnails made from them and Synology's copies in the photo's old `@eaDir`, and can't be undone.  Files deleted with a photo, such as a Live Photo's video, go with it either way.  Videos deleted on their own, and anything put in `deleted/` by hand rather than from the library, aren't shown; emptying the trash from `/manage` still removes them.
- Screenshots (below).
- `/library/cameras` has every camera with photos in the library, those with the most photos first, each with the years it was used and the list of its photos.  Photos with no camera in their details, such as screenshots and scans, are under No camera.  A photo's info card links its camera here.
- A folder for each tag, after those.

## Changing a date or place

A photo's info card has Change date, for an exact date and time, and Change place, for a town found by name or a point tapped on the map.  Either can go back to what the photo's file says, and each change can be undone.  In any month or folder, Select, then the calendar, opens a date picker that sets one date for many photos at once. It starts at the first photo's date in its file. When any of them had a date set by hand, the picker's Clear (Reset on an iPhone) takes them back to their files' dates. Place sets a town or a point tapped on its map for many photos at once, says they have none, or goes back to their own.

A place can also be given a name, such as Home or Cottage: open a photo taken there, then its info card, then Name this place, and choose how far around it counts, from 100 m to 5 km.  Every photo and video located within that distance, by its own location or one set by hand, is then at that place.  Named places come first on `/places` and in its search, show as circles on its map, appear on a photo's info card, and are offered first when setting a place by name: all of them as soon as the box is clicked, and then those with a word starting with what's typed, so "cot" finds Lake Cottage.  A named place's page has an Edit button for renaming it, changing its distance, or deleting the name.  They're kept in `place_labels.json` in the storage volume.

Dates set by hand go in `dates.csv` like those set by Select.  Places set by hand go in `locations_set.csv` by photo id, as a point or as having none, and take the place of what `locations.csv` says everywhere places are used: the info card, `/places` and its map.  Neither changes the original files.

## Tags

Tags are names given to photos by hand, each with a folder in the library.  The tag button in a photo's viewer shows who's in it and its tags: type in Add tag to tag it, or press × on a tag to take it off.  In any month or folder, Select, then Tag, adds a tag to many photos at once or takes it off.  A tag can also be made empty from the library, under Folders.  A tag's folder has an Edit button for renaming it, which joins it with another tag of that name, and for deleting it, which leaves the photos alone.  Tags are kept in `tags.db` in the storage volume, by photo id, so they survive a full rebuild.

The same row in the viewer has Add someone, for a person who's in a photo without a face to go on, such as one turned away.  They show on the person's page like any other photo of them, and in the viewer as a chip with no face, which can be removed or renamed like a face.  Select, then Tag, with Person chosen, does the same for many photos, and Take off removes only those tagged this way.  In `people.db` they're faces with no box and a score of 0, so they're never compared, grouped or offered for naming, and renaming, joining or removing a person carries them along.

## Screenshots

`/screenshots`, also in the library's folders, has every screenshot from an iPhone or iPad, for deleting in bulk: Select, then Select all, then untick the ones to keep.  iOS marks each screenshot in its `UserComment` tag, which `scripts/exiftool.config` turns into a `Screenshot` column in the index, so new ones show up after the scan that finds them.  Screenshots stay in their months in the library too.

Deleting from this page takes every copy of a screenshot, such as the same file backed up into two folders.  Elsewhere another copy takes a deleted photo's place.

## Finding faces

`/manage` can check the library for photos with faces in them.  It runs on the server, using [YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) on each photo's thumbnail, and no photos leave it.  It takes about 2 hours per 60,000 photos on a Celeron J4125.  It only notes that a face is there, not whose it is.

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

After the first time, each new face joins the group its closest faces are in, and gets a name when that's clear: its group has at least 5 named faces and 95% of them are one person, and at least 6 of its 10 closest named faces in other photos are that person, making up 90% of how alike they all are.  A name already in the photo, or one the face has been said not to be, isn't given.  Hiding the names given by hand and naming them this way, 77% of faces from 2024 on got a name and 3 of 1,947 were wrong; older photos fare worse, mostly by mixing up sisters as children.  Faces that don't get a name wait in "Who is this?" as before.

`/people` shows every group of faces that look alike in three tabs under "Who is this?", loading more as you scroll.  Suggested has the groups that look like someone already named, closest first, each with that name as a suggestion.  Groups has the rest, largest first, or tightest first: groups with photos from at least 4 different days, ordered by how alike the best face from each day is.  Going by days keeps a burst of photos taken a moment apart, which are as alike as faces get, from coming first.  Comparing every group with every named face runs in the background and takes about a minute on the NAS, so after you name someone the order and suggestions catch up on a later reload.

Map places a dot for every group and every named person with at least 5 faces, so that those that look alike are close together.  It uses t-SNE from scikit-learn on each one's average face, keeping each near its 30 most alike.  Each map starts from where the last one's dots were, kept in `people_map_layout.json` in the storage volume, and only settles them, so the map stays put as names and photos change: made afresh, one added dot of a thousand reshuffled it, leaving only 44% of each dot's 10 closest; started from the last one, 96% stayed.  It starts afresh when under half the dots were on the last map, or with no file.  Keeping each near its 300 most alike got more of the order of things far apart right (of three groups where one pair is clearly more alike, 77% against 70%) but kept under half as many of each group's 10 most alike close by, and looked worse.  UMAP, PaCMAP, MDS and openTSNE's multi-scale and exaggeration did no better on this library, and neither did placing dots along the two straight directions that best tell named people apart (linear discriminant analysis), which came out as mixes of age and sex and kept only 4% of each group's 10 most alike close by.  The map and the comparison behind Suggested are worked out after each scan and the face jobs after it, and after a restart, so they're ready before anyone looks.  When names change they're worked out again the next time someone looks, showing the last ones meanwhile.  Map is what "Who is this?" opens on.  Each person is one dot showing their cover, or with "One dot per person" turned off, a dot for each group their faces came from, which can show them as a child apart from grown up.  Clicking a dot opens its group or person, and pointing at one draws lines to those of its 5 most alike dots that are at least 20% alike, thicker the more alike they are.  Lines at 30% or more joined dots a median of 3% of the map's width apart, lines between 20 and 30% 15% apart, and weaker ones, which aren't drawn, 30% apart.  Unrelated faces' averages are 10 to 25% alike, so the thinnest lines are often strangers.  5% of groups have nothing 20% alike.  A slider hides unnamed groups with fewer faces than it says, from 5 up to 200, leaving named people.  Blood relatives are placed closer together than their faces alone would put them: the map takes the distance between the dots of a parent and child or two siblings as 30% of what their faces say, a grandparent, aunt, uncle, niece or nephew as 60%, and a first cousin or great-grandparent as 85%.  Partners and in-laws aren't pulled together, though two parents still end up near each other, as each is pulled towards their children.  t-SNE moves a dot only a little from where it starts, so each related dot also starts in the middle of its relatives' dots.  On the 910 dots of the one-dot-per-person map, with a made-up family of 14 people, 90% of parents and children and of siblings ended up among each other's 10 closest dots, against 24% without this, as did 71% of second-degree relatives and 33% of third-degree ones, against none, and the other dots kept 90% of their 10 closest.  That was on the map as worked out, but the browser then pushes overlapping dots apart, and a family packed into less room than its dots take came out shuffled: with a real family of 45, 13 of 49 parents and children were drawn touching.  So the server sends who is whose parent, and while the browser pushes dots apart it also pulls each parent and child, and at half the strength each two siblings, towards touching, using each person's biggest dot when there are several.  That made it 30 of 49, as many as fit around each other, and drawing took 86 ms instead of 133.  Two people with a child together are pulled together as hard as a parent and child.  Each two others in the same family with no ancestor in common, such as in-laws, are pushed apart while they're less than 150 pixels apart, with each person's push shared between all their in-laws, so that someone with many isn't pushed away from their partner.  That spreads a family out into its sides with the children between: the median gap between in-laws went from 178 pixels to 221, with 33 of 49 parents and children touching.  No dot moves more than 20 pixels a round, as stronger pulls overshot and grew until the drawing never finished.

A person's "Looks like" page has the same map as "Map of groups", showing only their groups and those whose average face is at least as alike to the closest of theirs as a slider says, 30% to start with.  Naming a group, or some of the faces in it, adds that person's photos to their own page.

Edit on a person's page renames them, or removes them, which sends their faces back to "Who is this?" and leaves the photos alone.  Using a name someone already has joins the two together.  All of these can be undone straight afterwards.

People can be marked as family, from Edit on their page or with "Choose family" on `/people`.  Once anyone is, `/people` shows only family, with "Everyone" to show friends and everyone else too.  Edit on a person's page also sets their mom and dad, kept in the `parents` table of people.db, and lists the relatives that follow from everyone's: their children, their brothers and sisters, who share a mom or dad, and those they have a child with.  Partners with no children together and siblings whose parents aren't named can't be shown this way.  Renaming someone takes their mom and dad along, and their children's mom or dad becomes the new name.

The viewer's person button draws a box around each face found in a photo, and a person's page starts out showing them.  Pressing a box or a face in the row below it gives that face a different name, takes its name off, skips it, or makes it the person's cover.  A person's Faces page also shows any face in its photo, with `?face=` and the face's id in the address, and selecting one there can make it their cover.  Faces pages, for people and for groups, can show one year at a time, and naming a whole group while looking at a year names only that year's faces.

A person's Faces page also has "May be someone else", which lists faces whose closest named faces in other photos mostly have a different name, grouped by that name.  It only suggests: nothing changes until you say who a face is, and a face you've settled doesn't come up again.  New names change what it finds.

"Looks like" is the same the other way round: faces not called this person, named or not, with at least 3 of their 10 closest named faces being this person and those making up most of them.  Relatives do come up: hiding every name of one person left about 5% of their faces matching someone else.  Saying a face without a name isn't this person doesn't stop it coming up for someone else.  Both comparisons run on the server when the page is opened, taking from a few seconds to a couple of minutes for someone with 16,000 faces on a Celeron J4125.  A person's cover is otherwise their largest, clearest face.

## Dogs

Dogs are named like people, and show up the same way on `/people` and in the viewer, as a box around the whole dog.  `scripts/find_dogs.py` runs after the people, on every photo that hasn't been looked at for dogs, about 10 seconds a photo on a Celeron J4125.  It finds dogs and cats with [YOLOX-m](https://github.com/Megvii-BaseDetection/YOLOX) and describes each with [DINOv2-small](https://github.com/facebookresearch/dinov2), both Apache 2.0, fetched when the image is built.  They're kept in `people.db` too.

For each dog with at least 5 named photos, a logistic regression on those descriptions is worked out afresh every time, from the dogs with that name against every other named dog, every cat, and every dog photographed more than half a year before that dog's first named photo or a year after its last.  A new dog gets a name when it's from around when that dog was photographed and the model is at least 90% sure.  Without the dates, a friend's dog of the same breed would pass; without the cats, a black cat passed for a black dog.  Names given or changed by hand count from the next run, as does a name given to a dog's face, once most of that name's faces are inside dogs.  A named dog whose name is taken off isn't named again.

Like faces, it only starts once a library has been gone through.  This one's was done on a faster computer with a different detector, YOLO11m, and named the same way.  Checked by eye, samples of 60 named photos of each dog were all right, while about 5 to 10% of those named with less than 97% certainty were another dog, two dogs in one box, or a cat.

## Places

`/places` finds photos and videos by where they were taken: a town, state or country, by what they recorded themselves, and any folder by its name, like `2002.03.11 - Escalante`.  Many phones and old cameras don't record a location, so folder names cover a lot of older photos.

`scripts/index_locations.py` reads locations into `locations.csv` in the storage volume at the end of each scan, only for files it hasn't seen, and works out the nearest town from a list of [GeoNames](https://www.geonames.org/) towns of 1,000 or more people (CC BY 4.0).  Nothing about a photo is sent anywhere.

`/places/map` shows the same locations on a map, with whatever's in view listed alongside.  The map itself comes from [OpenStreetMap](https://www.openstreetmap.org/), so browsers viewing it ask them for the areas being looked at.

## Info cards

The info button on a photo, or `i` in the viewer, shows who's in it, where it was taken, its camera, exposure and file details, and every tag in the index.  A photo with a camera's raw file behind it, such as a JPEG made from a CR2 with the same name and time, is shown from the other file, and its card names the raw file under File with a link to download it.  Its camera, lens and exposure come from the raw file, since an export can lose them: 84 JPEGs of the Canon EOS 20D's photos had none of their own, and now count under that camera on `/library/cameras`.  The card also gives the raw file's own dimensions and size beside the shown file's.  In the viewer, such a photo has a RAW button that shows the raw file instead, developed on the NAS from what the sensor recorded, at screen size (`/api/raw`, cached like other copies; about 1.4 seconds the first time for a 20D's CR2).  The preview inside a raw file isn't used, since an older Canon's is 1536 pixels across.  RAW stays on from photo to photo until the viewer closes, the address gets `&raw=1`, and download takes the raw file.  Face boxes are hidden while it's on, since they were found in the other file, which may be cropped.  In the library, a mouse also gets favorite, rotate, hide, delete and info buttons on hovering over a thumbnail.

## Videos

`/movies`, Videos in the navigation bar, plays every home video in the order it was filmed, as one continuous stream: each video runs straight into the next, across months and years.  Under the player, the videos are listed newest first by day, the same way as the library, with the same year and month boxes at the top for going to a date.  Pick a video to drop in there; the stream itself always plays forward in time, and the scrubber covers the month being played.  It picks up where it left off on that device.  The address names the video playing (`/movies?path=`), so it can be copied and shared, and a reload carries on from the same moment.

The list and the stream can be narrowed to a tag and to a place, as the library can.  Videos are tagged from the row under the player, which shows the tags of the one playing.

Keyboard: Space plays and pauses, ←/→ moves 10 seconds and carries over into the next or previous video, Shift+←/→ moves by video, ↑/↓ moves by month, F is full screen, M mutes and R rotates.

Safari shows an AirPlay button when it can see a receiver.  The receiver fetches each video itself, so it needs to reach the same address as the page: a `ts.net` name only resolves on the tailnet, so an Apple TV needs Tailscale installed or a local DNS entry for that name.

Videos are read by the end of each scan into `movies.csv` in the storage volume, which is safe to delete: the next scan rebuilds it, though reading every video takes a while (about 20 minutes for 11,000 on a Synology DS224+).  After that a scan only reads new ones.  Live Photos' motion clips, the same clip filed in several folders, and anything in `videos/Work movies` are left out.  Where a video's name starts with a date, like `2009.10 Lucy's Birth.mp4`, or its folder's does, that says when it was filmed and is shown as its title, since an edited video's own date is when it was exported.

Rotating turns a video a quarter turn clockwise each press, for one filmed sideways (stored in `movie_rotations.csv` by video).  The player turns the video and its pictures as it shows them, and the file isn't changed, so AirPlay and shared links still play it as filmed.

Hiding takes a video out of the stream (stored in `movies_hidden.csv`) and can be undone from the list at the bottom of the page.  Deleting takes two presses and moves the video and any other copies of it to `deleted/` in the storage volume, with the same undo and `/manage` trash as photos.  A same-named photo is left alone.  A large video can take minutes to reach the trash, as it's copied there, and only then can the delete be undone.

Browsers get the original where they can play it, which is any H.264 and also HEVC if the browser has it.  Otherwise they get a copy Synology made in `@eaDir`, such as for old MPEG-4 and AVI files, and nothing is converted here.  A video with neither is skipped with a message.

## Duplicates

After a scan changes the index, the server looks for photos that are copies of each other, such as a screenshot of a photo that was then texted around.  Copies it's sure about are hidden automatically, keeping the best one.  A camera's own photo, with its camera and date, always comes first, so a DSLR's file wins over any copy made from it.  An iPhone's edit (`IMG_E…`) wins over the photo it was made from, even cropped, since someone chose it.  After that it's the most detail, then one that isn't a screenshot, one with a date, and one in a named folder.  The copy that stays takes on any approval or favorite the others had, so nothing drops out of the kiosk.  Nothing is deleted.

Only small copies count, like those from texts or Facebook.  A full-size export of a photo from a camera that isn't a phone, such as a JPEG of a raw file, is kept as a photo of its own: it has at least half the camera file's pixels and at least 4 million.  An export with its own tone, vignette or lens correction differs block by block like a different photo, so a copy without camera details that lines up with a camera's photo on at least 400 matching points waits for a decision even then.  Below 400, about half of such pairs on a contact sheet of the library were the next shot in a burst.  A small copy, with a quarter of a camera photo's pixels or fewer and framed like it, is hidden automatically however it was edited, if it lines up on at least 600 points or passes the block by block comparison.  The first run of that hid 249 copies, all of them the same photo on a contact sheet.  Sizes come from exiftool's `ImageSize`, because the `ImageWidth` and `ImageHeight` of an older Canon's CR2 file, like the 20D's, are those of the small preview inside it.  Until that was fixed, 29 such raw files were hidden in favor of a full-size JPEG of them.  When a newer check makes a hidden copy the best one, that copy comes back in place of the one kept, but not if a person hid it.  Keeping a copy writes the copies it hides at the same moment, and that's how the check tells a person's decision from its own.

Photos that may be copies but aren't certain, like annotated or brighter versions, wait for a decision at the top of `/duplicates`, one group at a time, newest first.  Each copy is shown large with its size, file, date, camera, folder, name and status, and whatever differs between them is picked out.  The best copy starts marked to keep and the rest to hide.  A number key (or Keep only this) keeps just that copy, Shift and a number (or a click on a photo) changes its mark, Enter keeps the marked ones, N says they aren't duplicates, ←/→ go back or skip one for now, and U undoes the last decision, back to how each photo was.  Each decision saves in the background, so the next group shows at once.  On a phone the decisions sit in a bar at the bottom of the screen.  The first copy kept takes on any approval or favorite the hidden ones had.

Below that, Hidden copies lists every group with a copy hidden, automatically or by a decision above, a page at a time as the list is scrolled.  Another copy can be kept instead or a hidden one brought back, without reloading the page, and either sticks through later checks.  Shots from a camera's burst or timer are never treated as copies, and a copy is only hidden if it matches the one that's kept.

The photo viewer's compare button, with a count of what may be copies of the open photo, opens the same review over the viewer for that photo and those copies, with the same keys, Esc to close, and the best copy marked by the same rules.  Candidates are every photo whose fingerprint is within 10 of 64 bits of this one's, and any the check grouped with it, including copies already hidden.  Those the check found different, or full-size exports, are left out unless asked for, and each says what the check concluded.  Whatever is hidden there leaves the library behind it at once, and comes back with an undo.

Each photo's fingerprint comes from its thumbnail, and only photos with similar fingerprints get compared, by lining them up and comparing them block by block.  Results are kept in `duplicate_fingerprints.csv` and `duplicate_pairs.csv` in the storage volume, so later checks only do what's new.  `duplicate_rules.json` says which rules those were judged by, and when the rules change the check runs again by itself, looking again only at pairs the change affects.  What's been dealt with is in `duplicates.csv`.  `POST /api/duplicates/reset` takes back every decision about copies, automatic or not, and makes them again by the current rules.  Each copy hidden comes back with the review it had before, unless someone reviewed it since, then what the check is sure about is hidden again and the rest wait on `/duplicates`.  What a kept copy took over from hidden ones, such as an approval, stays.  The first check takes a little over an hour per 60,000 photos on a Celeron J4125.

## The kiosk

`/kiosk` shows approved photos from around today's date in any year, taking turns with photos from today and the 3 days before it.  A recent photo doesn't need approving first: it shows unless it's hidden, and of the unreviewed ones the frame shows what the review queue would start checked, so one photo from each burst and no screenshots.  An unreviewed photo leaves the frame once it's no longer recent, unless someone approves or favorites it.  When too few photos are from today, the frame fills in from nearby dates and then favorites, except while today's date has photos to review: then it shows only today's approved photos, leaving out recent ones too, so the ones waiting aren't lost among other days'.  A date with none approved says how many are waiting, with a link to review them, over favorites going by dimmed behind it.

The photo's controls go back to the photo before, pause, and move on to the next, and have an approve button while it's unreviewed.  Approving or hiding decides the rest of its burst the way the review queue does, and both can be undone.  The "to review" link counts unreviewed photos from today's date in any year and from the recent days, including the screenshots and burst photos the frame leaves out.  It opens a review queue of those same photos, which goes back to the slideshow when they've all been decided.

`/kiosk` is meant to run unattended for days.  It keeps showing photos through server outages and picks up again by itself, updates when photos change or a new version is deployed, and reloads at midnight.  Once it has loaded over HTTPS, a service worker turns any failed page load in that browser into a screen that keeps retrying, including after a restart where the frame comes up before the server does.

## Images

Pages show copies sized for the screen, and downloads give the original file.  On a Synology, the copies Synology has already made in `@eaDir` are used where they're up to date.  Anything else is converted once, limited to `MAX_IMAGE_SIZE`, and cached in `thumbs/`.
