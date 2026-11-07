import asyncio
import gzip
import os
import datetime
import hashlib
import itertools
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, Query, Request, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from api import duplicates, exif, faces, info, movies, people, places, scans, tags, thumbs, trash


@asynccontextmanager
async def lifespan(app):
    try:
        people.carry_over_old_names()
    except Exception as e:
        logger.warning(f"Can't carry over old names: {e}")
    task = asyncio.create_task(check_photos_after_scans())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(APP_DIR, "static")
TEMPLATES_DIR = os.path.join(APP_DIR, "templates")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)
# for a person's Edit panel, which is on more than one page
templates.env.globals['is_family'] = lambda name: name in people.family()
templates.env.globals['family_of'] = lambda name: {'parents': people.parents_of(name), **people.relatives(name)}
# for suggesting tags wherever photos can be tagged
templates.env.globals['tag_names'] = tags.names
templates.env.globals['earliest_year'] = exif.LIBRARY_EARLIEST_YEAR


@app.middleware("http")
async def _check_static_files(request: Request, call_next):
    # without this, Safari went on using the stylesheet from before a deploy until a hard refresh.
    # Asking each time costs a quick "not modified" answer.  What's vendored never changes
    response = await call_next(request)
    path = request.url.path
    if path.startswith('/static/') and not path.startswith('/static/vendor/'):
        response.headers['Cache-Control'] = 'no-cache'
    return response


exif.load_csv()
scans.start_scheduler()
DEFAULT_NUM_IMAGES = int(os.getenv('DEFAULT_NUM_IMAGES', 20))
ReviewStatus = Literal['unreviewed', 'rejected', 'reviewed']
logger = logging.getLogger('uvicorn.error')
# lets a browser reuse a photo instead of having the NAS read and convert it again
IMAGE_CACHE_HEADERS = {'Cache-Control': 'private, max-age=86400'}
Rotation = Literal[0, 90, 180, 270]
# how many photos from other dates fill in a frame with too few from around today
KIOSK_STAND_INS = 200
# how many days before today count as recent.  The kiosk shows photos from then without waiting
# for them to be approved, up to a limit for the odd day with hundreds of photos
KIOSK_RECENT_DAYS = 3
KIOSK_RECENT_PHOTOS = 200
# how often to see whether a scan has finished, leaving photos to check for faces
SCAN_RESULT_POLL_SECS = 60


def page_build():
    # the same in every worker and across restarts, so that a page only reloads for a real change
    digest = hashlib.sha1()
    for directory in (STATIC_DIR, TEMPLATES_DIR):
        for folder, folders, names in sorted(os.walk(directory)):
            for name in sorted(names):
                with open(os.path.join(folder, name), 'rb') as fh:
                    digest.update(fh.read())
    return digest.hexdigest()


PAGE_BUILD = page_build()


def most_recent_date(month, day):
    # the latest past date with this month and day; Feb 29 doesn't exist every year
    today = datetime.date.today()
    year = today.year
    while True:
        try:
            date = datetime.date(month=month, day=day, year=year)
            if date <= today:
                return date
        except ValueError:
            pass
        year -= 1


def rotation_param(rotate: int = 0):
    if rotate not in (0, 90, 180, 270):
        raise HTTPException(status_code=422, detail="rotate must be 0, 90, 180 or 270")
    return rotate


def background_url():
    photo = exif.background_photo()
    if photo is None:
        return None
    path, rotation = photo
    return f"/api/image{quote(path)}" + (f"?rotate={rotation}" if rotation else "")


def library_path(path):
    normalized = os.path.normpath(path)
    if not normalized.startswith(f"{exif.LIBRARY_ROOT}/"):
        raise HTTPException(status_code=404)
    return normalized


def cached_image(make, path, rotation):
    path = library_path(path)
    try:
        cached = make(path, rotation)
    except Exception as e:
        # decoders each raise their own errors; pages show a placeholder or move on instead
        logger.warning(f"Can't make an image from {path}: {e}")
        raise HTTPException(status_code=404, detail="Image not available")
    return FileResponse(cached, media_type='image/jpeg', headers=IMAGE_CACHE_HEADERS)


def date_filter(date, all_years):
    # the DateTimeOriginal prefix for a date; no date means the whole library
    if date is None:
        return ''
    if all_years:
        return f"%{date.month:02d}:{date.day:02d} "
    return f"{date.year:04d}:{date.month:02d}:{date.day:02d} "


def recent_range(days):
    # today and the days before it, as exif.RECENT_FILTER takes them
    if not days:
        return None
    today = datetime.date.today()
    first = today - datetime.timedelta(days=days)
    after = today + datetime.timedelta(days=1)
    return first.strftime('%Y:%m:%d'), after.strftime('%Y:%m:%d')


def review_url(date, all_years, status='unreviewed', kiosk=None, recent_days=0):
    params = {'status': status}
    if date:
        params.update(date=date.isoformat(), all_years=str(all_years).lower())
    if recent_days:
        params['recent_days'] = recent_days
    # the kiosk's own query string, so that reviewing can return to the same slideshow
    if kiosk is not None:
        params['kiosk'] = kiosk
    return f"/review?{urlencode(params)}"


def sample_across_years(pics, n):
    """
    A random n photos, taking turns between years, so that a date isn't dominated by
    whichever years happen to have the most photos
    """
    shuffled = pics.sample(frac=1)
    turn = shuffled.groupby(shuffled['DateTimeOriginal'].str[:4]).cumcount()
    return shuffled.assign(turn=turn).sort_values('turn', kind='stable').head(n).drop(columns='turn')


def gallery(request, date, all_years, n):
    datestring = date_filter(date, all_years)
    counts = exif.review_counts(datestring)
    pics = exif.select_photos(datestring)
    if len(pics) > n:
        pics = sample_across_years(pics, n)

    return templates.TemplateResponse(
        request=request,
        name="gallery.html",
        context={
            "picked_date": date.isoformat(),
            "all_years": all_years,
            "counts": counts,
            "background": None if counts[exif.APPROVED] else background_url(),
            "review_url": review_url(date, all_years),
            "edit_url": review_url(date, all_years, status='reviewed'),
            "images": pics.assign(
                date=pics['DateTimeOriginal'].map(
                    lambda d: d.split(' ')[0].replace(':', '/')
                )
            ).sort_values(
                by='DateTimeOriginal',
                ascending=False
            ).to_dict(orient='records')
        }
    )


@app.get("/api/ping")
async def _ping():
    return "pong"


@app.get("/api/reload")
async def _reload():
    exif.load_csv()
    return f"Last modified: {exif.last_modified()}"


@app.get("/api/scan")
async def _scan_status():
    return {
        **scans.status(),
        'indexed_photos': exif.count(),
        'indexed_movies': len(movies.clips()),
        'reviews': exif.review_counts()
    }


@app.post("/api/scan")
async def _start_scan(full: bool = False):
    if not scans.start(full):
        raise HTTPException(status_code=409, detail="A scan is already running")
    return "started"


@app.delete("/api/scan")
async def _cancel_scan():
    if not scans.cancel():
        raise HTTPException(status_code=409, detail="No scan that can be cancelled from here")
    return "cancelling"


@app.put("/api/scan/schedule")
async def _set_scan_schedule(
    hours: int,
    at: str = Query(default=None, pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
):
    if hours < 0:
        raise HTTPException(status_code=422, detail="hours can't be negative")
    scans.set_schedule(hours, at)
    return scans.next_scan()


@app.get("/manage")
async def _manage(request: Request):
    return templates.TemplateResponse(request=request, name="manage.html")


class RestoreRequest(BaseModel):
    files: list[str]


@app.delete("/api/image{path:path}")
async def _delete_image(path: str, every_copy: bool = False):
    path = library_path(path)
    # found here on the event loop, the only place the index's connection is used
    paths = exif.copies_of(path) if every_copy else [path]
    return await run_in_threadpool(discard_photo, path, paths)


def discard_photo(path, paths):
    files = [moved for each in paths for moved in trash.discard(each)]
    exif.record_deleted({path, *files})
    return {'files': files}


@app.post("/api/restore")
async def _restore_images(request: RestoreRequest):
    restored = await run_in_threadpool(trash.restore, [library_path(f) for f in request.files])
    if not restored:
        raise HTTPException(status_code=404, detail="Nothing to restore")
    exif.record_restored(restored)
    # one deleted before the last scan has dropped out of the index until the next one
    return {'files': restored, 'indexed': exif.indexed(restored)}


@app.get("/api/trash")
def _trash():
    return trash.summary()


@app.post("/api/trash/purge")
def _purge(request: RestoreRequest):
    purged = trash.purge([library_path(f) for f in request.files])
    if not purged:
        raise HTTPException(status_code=404, detail="Nothing to delete")
    return {'files': purged}


# for a photo in the trash, by where it was in the library.  Not async, as with the library's
@app.get("/api/trash/thumb{path:path}")
def _trash_thumb(path: str, rotate: int = Depends(rotation_param)):
    return cached_image(lambda found, rotation: thumbs.thumbnail(trash.trashed(found), rotation, found), path, rotate)


@app.get("/api/trash/image{path:path}")
def _trash_image(path: str, rotate: int = Depends(rotation_param)):
    return cached_image(lambda found, rotation: thumbs.display(trash.trashed(found), rotation, found), path, rotate)


@app.delete("/api/trash")
def _empty_trash():
    trash.empty()
    return trash.summary()


# not async: decoding is slow, and would otherwise hold up every other request
@app.get("/api/image{path:path}")
def _image(path: str, rotate: int = Depends(rotation_param)):
    return cached_image(thumbs.display, path, rotate)


@app.get("/api/original{path:path}")
def _original(path: str):
    path = library_path(path)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, filename=os.path.basename(path))


# not async: decoding is slow, and would otherwise hold up every other request
@app.get("/api/thumb{path:path}")
def _thumb(path: str, rotate: int = Depends(rotation_param)):
    return cached_image(thumbs.thumbnail, path, rotate)


@app.get("/api/tags")
async def _tags():
    return exif.tags()


@app.get("/api/exif")
async def _exif(source_file: str):
    rows = exif.db.execute(
        "SELECT * FROM exif WHERE SourceFile = ?",
        [source_file]
    ).fetchdf().fillna('').to_dict(orient='records')
    if not rows:
        raise HTTPException(status_code=404)
    return rows[0]


@app.get("/api/dump")
async def _dump():
    def iterfile():  #
        with open(exif.EXIF_CSV, mode="rb") as fh:
            yield from fh

    return StreamingResponse(
        iterfile(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=exif-db.csv"}
    )


@app.get("/onthisday")
async def _onthisday(
    request: Request,
    month: int = -1,
    day: int = -1,
    year: int = None,
    all_years: bool = True,
    n: int = DEFAULT_NUM_IMAGES
):
    if min(month, day) <= 0:
        date = datetime.date.today()
    elif year:
        date = datetime.date(month=month, day=day, year=year)
    else:
        date = most_recent_date(month, day)
    return gallery(request, date, all_years, n)


class ReviewDecisions(BaseModel):
    approved: list[str] = []
    rejected: list[str] = []
    unreviewed: list[str] = []


class FavoriteChange(BaseModel):
    photo_id: str
    favorite: bool


class FavoritesChange(BaseModel):
    photo_ids: list[str]
    favorite: bool


class RotationChange(BaseModel):
    photo_id: str
    rotation: Rotation


class RotationsChange(BaseModel):
    rotations: dict[str, Rotation]


@app.post("/api/rotate")
async def _set_rotation(change: RotationChange):
    exif.set_rotations({change.photo_id: change.rotation})
    return "saved"


@app.post("/api/rotations")
async def _set_rotations(change: RotationsChange):
    exif.set_rotations(change.rotations)
    return "saved"


@app.post("/api/favorite")
async def _set_favorite(change: FavoriteChange):
    exif.set_favorites([change.photo_id], change.favorite)
    return "saved"


@app.post("/api/favorites")
async def _set_favorites(change: FavoritesChange):
    exif.set_favorites(change.photo_ids, change.favorite)
    return "saved"


@app.get("/api/review")
async def _review_page(
    date: datetime.date = None,
    all_years: bool = True,
    status: ReviewStatus = 'unreviewed',
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=24, ge=1, le=200),
    recent_days: int = Query(default=0, ge=0, le=31)
):
    return exif.review_page(date_filter(date, all_years), status, offset, limit, recent_range(recent_days))


def kiosk_to_review(date):
    # the same photos the kiosk's link to the review queue goes through
    return exif.review_counts(date_filter(date, True), recent_range(KIOSK_RECENT_DAYS))[exif.UNREVIEWED]


@app.get("/api/review/today")
async def _review_today():
    date = datetime.date.today()
    return {
        'date': date.isoformat(),
        'unreviewed': kiosk_to_review(date),
        'photos': exif.photos_version(),
        'build': PAGE_BUILD
    }


@app.post("/api/review")
async def _record_reviews(decisions: ReviewDecisions):
    exif.record_reviews(decisions.approved, decisions.rejected, decisions.unreviewed)
    return "saved"


@app.delete("/api/review")
async def _reset_reviews(date: datetime.date = None, all_years: bool = True):
    return {'reset': exif.reset_reviews(date_filter(date, all_years))}


@app.get("/review")
async def _review(
    request: Request,
    date: datetime.date = None,
    all_years: bool = True,
    status: ReviewStatus = 'unreviewed',
    kiosk: str = None,
    recent_days: int = Query(default=0, ge=0, le=31)
):
    if date is None:
        scope = "All dates"
        done_url = "/library" if status == 'unreviewed' else "/manage"
    else:
        scope = f"{date:%B} {date.day}" + ("" if all_years else f", {date.year}")
        if recent_days:
            scope += f" and the past {recent_days} days"
        done_url = "/onthisday?" + urlencode({
            'year': date.year,
            'month': date.month,
            'day': date.day,
            'all_years': str(all_years).lower()
        })
    if kiosk is not None:
        done_url = f"/kiosk?{kiosk}" if kiosk else "/kiosk"
    return templates.TemplateResponse(
        request=request,
        name="review.html",
        context={
            "scope": scope,
            "status": status,
            "done_url": done_url,
            "kiosk": kiosk is not None,
            "query": {
                'status': status,
                'all_years': all_years,
                **({'date': date.isoformat()} if date else {}),
                **({'recent_days': recent_days} if recent_days else {})
            }
        }
    )


def library_query(with_faces):
    return '?with_faces=true' if with_faces else ''


def library_url(month, with_faces):
    return '/library/' + (month.replace(':', '/') if month else 'undated') + library_query(with_faces)


def library_tile(entry, with_faces):
    # an entry from exif.library_months, with what the page shows for it
    year, number = entry['month'].split(':') if entry['month'] else (None, None)
    return {
        **entry,
        'year': year,
        'name': datetime.date(2000, int(number), 1).strftime('%B') if number else 'Undated',
        'url': library_url(entry['month'], with_faces)
    }


def screenshots_tile():
    photos = exif.screenshots()
    if not photos:
        return None
    return {'name': 'Screenshots', 'photos': len(photos), 'cover': photos[0], 'url': '/screenshots'}


def folder_tiles(with_faces, binned):
    """
    The library's virtual folders, which gather photos from every month.  These show even when
    empty, so that it's clear where to look
    """
    def tile(name, url, photos, icon):
        return {'name': name, 'url': url, 'photos': len(photos), 'cover': photos[0] if photos else None, 'icon': icon}

    tiles = [
        tile('Favorites', '/library/favorites', exif.favorites(), 'fa-heart'),
        tile('Hidden', '/library/hidden', exif.hidden_photos(), 'fa-eye-slash'),
        {**tile('Recycle bin', '/library/recycle-bin', binned, 'fa-trash-o'), 'trash': True}
    ]
    # not with faces, as nobody wants a screenshot for its faces
    screenshots = None if with_faces else screenshots_tile()
    cameras = exif.cameras()
    tiles.append({
        'name': 'Cameras',
        'url': '/library/cameras',
        'photos': len(cameras),
        'cover': cameras[0]['cover'] if cameras else None,
        'icon': 'fa-camera'
    })
    tagged = [
        {**tile(name, tag_url(name), exif.photos_with_ids(tags.photo_ids(name)), 'fa-tag'), 'tag': True}
        for name in tags.names()
    ]
    return tiles + ([screenshots] if screenshots else []) + tagged


def tag_url(name):
    return f"/library/tags/{quote(name, safe='')}"


def library_years(tiles):
    dated = (tile for tile in tiles if tile['year'])
    years = []
    for year, group in itertools.groupby(dated, key=lambda tile: tile['year']):
        months = list(group)
        years.append({'year': year, 'months': months, 'photos': sum(tile['photos'] for tile in months)})
    return years


def folder_date(path, dated_folders):
    """
    What a folder's name says about when its photos were taken, as a date and how to describe
    it, such as for "2002/2002.03.11 - Escalante", "2008.10 - Trip", "2024/04" or "2003 - Wedding".
    A bare year and month only counts if other photos there are really from that month: they can
    also be where a tool filed photos with nothing else to go on
    """
    if 'unsorted' in path.lower():
        return None
    parts = path.split('/')
    for position, part in reversed(list(enumerate(parts))):
        parent = parts[position - 1] if position else ''
        day = re.match(r'(\d{4})[.-](\d{2})[.-](\d{2})\b', part)
        month = re.match(r'(\d{4})[.-](\d{2})\b', part)
        filed_by_month = re.fullmatch(r'\d{2}', part) and re.fullmatch(r'\d{4}', parent)
        year = re.fullmatch(r'(\d{4})( - .*)?', part)
        try:
            if day:
                date = datetime.date(int(day[1]), int(day[2]), int(day[3]))
                label = f"{date:%B} {date.day}, {date.year}"
            elif month:
                date = datetime.date(int(month[1]), int(month[2]), 1)
                label = f"{date:%B %Y}"
            elif filed_by_month:
                if (path, f"{parent}:{part}") not in dated_folders:
                    return None
                date = datetime.date(int(parent), int(part), 1)
                label = f"{date:%B %Y}"
            elif year:
                date = datetime.date(int(year[1]), 1, 1)
                label = f"{date.year}, as January 1"
            else:
                continue
        except ValueError:
            continue
        if valid_photo_date(date):
            return {'date': date.isoformat(), 'label': label}
    return None


def valid_photo_date(date):
    return exif.LIBRARY_EARLIEST_YEAR <= date.year and date <= datetime.date.today()


def library_photo(photo):
    # with its date spelled out; a date's day can still be impossible, like Feb 30
    day = when = None
    try:
        taken = datetime.datetime.strptime(photo['taken'][:19], '%Y:%m:%d %H:%M:%S')
        day = f"{taken:%A}, {taken:%B} {taken.day}"
        when = f"{day}, {taken.year}"
        # the time of a date given by hand doesn't mean anything
        if not photo['date_assigned']:
            when += f" · {taken.hour % 12 or 12}:{taken:%M %p}"
    except ValueError:
        pass
    return {**photo, 'day': day, 'when': when}


def undated_photo(photo, dated_folders):
    # undated photos are laid out by folder, which is the best clue to when they're from
    folder = os.path.dirname(photo['path'])
    return {
        **photo,
        'day': os.path.relpath(folder, exif.LIBRARY_ROOT),
        'when': None,
        'suggested': folder_date(folder, dated_folders)
    }


def library_month_photos(month, with_faces):
    photos = exif.library_photos(month, with_faces)
    if month is None:
        dated_folders = exif.dated_folders()
        by_folder = sorted((undated_photo(photo, dated_folders) for photo in photos), key=lambda photo: photo['day'])
        # newest first like everything else, by what a folder's name suggests; those with no date go last
        return sorted(by_folder, key=lambda photo: (photo['suggested'] or {}).get('date', ''), reverse=True)
    return list(map(library_photo, photos))


def library_month_page(request, month, title, with_faces):
    months = [entry['month'] for entry in exif.library_months(with_faces)]
    # a month can empty out while its page is open, such as by deleting everything in it
    if month not in months:
        return RedirectResponse(url=f"/library{library_query(with_faces)}")
    position = months.index(month)
    return templates.TemplateResponse(
        request=request,
        name="library_month.html",
        context={
            "title": title,
            "dated": month is not None,
            "with_faces": with_faces,
            "faces_checked": exif.face_counts()['checked'],
            "back_url": f"/library{library_query(with_faces)}",
            "back_label": "Library",
            "other_filter_url": library_url(month, not with_faces),
            "month": month.replace(':', '-') if month else None,
            "today": datetime.date.today().isoformat(),
            "earliest": f"{exif.LIBRARY_EARLIEST_YEAR}-01-01",
            "photos": library_month_photos(month, with_faces),
            "known_names": people.known_names(),
            "newer_url": library_url(months[position - 1], with_faces) if position else None,
            "older_url": library_url(months[position + 1], with_faces) if position + 1 < len(months) else None,
            # what scrolling off the end carries on into: not undated photos, which go by folder
            "next_url": library_url(months[position + 1], with_faces) if month and position + 1 < len(months) and months[position + 1] else None
        }
    )


@app.get("/library")
async def _library(request: Request, with_faces: bool = False):
    tiles = [library_tile(entry, with_faces) for entry in exif.library_months(with_faces)]
    return templates.TemplateResponse(
        request=request,
        name="library.html",
        context={
            "total": sum(tile['photos'] for tile in tiles),
            "with_faces": with_faces,
            "years": library_years(tiles),
            "undated": next((tile for tile in tiles if not tile['year']), None),
            "folders": folder_tiles(with_faces, await trash_photos())
        }
    )


@app.get("/library/favorites")
async def _favorites(request: Request, year: str = None):
    photos = exif.favorites()
    return photos_by_year_page(
        request,
        "Favorites",
        photos,
        "/library/favorites",
        year,
        back_url="/library",
        back_label="Library",
        folder='favorites',
        hint=None if photos else "No favorites yet. The heart on a photo adds it here."
    )


@app.get("/library/tags/{name}")
async def _tag(request: Request, name: str, year: str = None):
    if not tags.exists(name):
        return RedirectResponse(url='/library#folders')
    photos = exif.photos_with_ids(tags.photo_ids(name))
    return photos_by_year_page(
        request,
        name,
        photos,
        tag_url(name),
        year,
        back_url="/library#folders",
        back_label="Library",
        folder='tag',
        tag=name,
        hint=None if photos else (
            "No photos have this tag yet. To add some, open a photo and press the tag button, or "
            "select photos in any month and use Tag."
        )
    )


def camera_name(make, model):
    """
    What to call a camera: its model if that already says who made it, as "NIKON D90" does for
    "NIKON CORPORATION", or else both, as in "Apple iPhone 15 Pro"
    """
    if not make and not model:
        return 'No camera'
    if not make or not model:
        return make or model
    folded = model.casefold()
    if any(len(word) > 2 and word in folded for word in make.casefold().split()):
        return model
    return f"{make} {model}"


@app.get("/library/cameras")
async def _cameras(request: Request):
    cameras = [
        {
            **camera,
            'name': camera_name(camera['make'], camera['model']),
            'url': info.camera_url(camera['make'], camera['model']),
            'years': (
                None if not camera['first'] else
                camera['first'] if camera['first'] == camera['last'] else f"{camera['first']} to {camera['last']}"
            )
        }
        for camera in exif.cameras()
    ]
    return templates.TemplateResponse(request=request, name="cameras.html", context={"cameras": cameras})


@app.get("/library/camera")
async def _camera(request: Request, make: str = None, model: str = None, year: str = None):
    photos = exif.camera_photos(make or None, model or None)
    if not photos:
        return RedirectResponse(url='/library/cameras')
    name = camera_name(make, model)
    return photos_by_year_page(
        request,
        name,
        photos,
        info.camera_url(make, model),
        year,
        back_url="/library/cameras",
        back_label="Cameras",
        hint="Photos with no camera in their details, such as screenshots, scans and saved pictures." if not make and not model else None
    )


@app.get("/library/hidden")
async def _hidden(request: Request, year: str = None):
    photos = exif.hidden_photos()
    return photos_by_year_page(
        request,
        "Hidden",
        photos,
        "/library/hidden",
        year,
        back_url="/library",
        back_label="Library",
        folder='hidden',
        hint=(
            "Photos hidden from the library or left unchecked in the review queue. Unhiding one "
            "puts it back in the library, unreviewed. Copies hidden as duplicates are on the "
            "Duplicates page."
        ) if photos else "Nothing is hidden. Copies hidden as duplicates are on the Duplicates page."
    )


async def trash_photos():
    """
    What's in the trash from the library, newest deletion first.  When each was deleted comes from
    the log of deletions if it's there, as the trash's own times change if it's copied
    """
    # going through the trash is slow on a NAS, unlike the index, which is only used from here
    photos = await run_in_threadpool(trash.library_photos)
    rotations = exif.rotations_by_path(photo['path'] for photo in photos)
    logged = exif.deletion_times()
    photos = [
        {
            **photo,
            'rotation': rotations.get(photo['path']) or 0,
            'deleted': max((logged[f] for f in photo['files'] if f in logged), default=photo['deleted'])
        }
        for photo in photos
    ]
    return sorted(photos, key=lambda photo: (-photo['deleted'], photo['path']))


def trash_photo(photo):
    deleted = datetime.datetime.fromtimestamp(photo['deleted'])
    day = f"{deleted:%A}, {deleted:%B} {deleted.day}, {deleted.year}"
    return {
        # unique, and nothing else here goes by a photo's id
        'id': photo['path'],
        'path': photo['path'],
        'files': photo['files'],
        'rotation': photo['rotation'],
        'status': None,
        'favorite': False,
        'date_assigned': False,
        'day': f"Deleted {day}",
        'when': f"Deleted {day} · {deleted.hour % 12 or 12}:{deleted:%M %p}"
    }


@app.get("/library/recycle-bin")
async def _recycle_bin(request: Request):
    photos = [trash_photo(photo) for photo in await trash_photos()]
    return templates.TemplateResponse(
        request=request,
        name="library_month.html",
        context={
            "title": "Recycle bin",
            "dated": True,
            "month": None,
            "with_faces": False,
            "faces_checked": 0,
            "back_url": "/library",
            "back_label": "Library",
            "folder": 'trash',
            "photos": photos,
            "newer_url": None,
            "older_url": None,
            "next_url": None,
            "hint": (
                "Deleted photos stay here until they're deleted again from here or the trash is "
                "emptied from Manage. Deleting from here can't be undone."
            ) if photos else "Nothing has been deleted."
        }
    )


@app.get("/screenshots")
async def _screenshots(request: Request, year: str = None):
    photos = exif.screenshots()
    return photos_by_year_page(
        request,
        "Screenshots",
        photos or [],
        "/screenshots",
        year,
        back_url="/library",
        back_label="Library",
        # or another copy, such as one in a different backup folder, would take its place
        delete_every_copy=True,
        hint=(
            "Screenshots show up here after the next scan, which reads every photo again to find them."
            if photos is None else
            None if photos else "No screenshots."
        )
    )


@app.get("/library/undated")
async def _library_undated(request: Request, with_faces: bool = False):
    return library_month_page(request, None, "Undated", with_faces)


@app.get("/library/{year}/{month}")
async def _library_month(request: Request, year: int, month: int, with_faces: bool = False):
    try:
        title = f"{datetime.date(year, month, 1):%B} {year}"
    except ValueError:
        return RedirectResponse(url=f"/library{library_query(with_faces)}")
    return library_month_page(request, f"{year:04d}:{month:02d}", title, with_faces)


def check_photos_after_scan():
    """
    Once a scan has succeeded, check for faces in anything that hasn't been checked, which is
    what it added along with anything an earlier check didn't get to.  This waits for its turn,
    since a check already running was given its photos before the scan found the new ones
    """
    result = scans.last_result()
    if not result or result.get('exit_code') != 0 or result['finished'] <= faces.last_scan_checked():
        return
    if scans.running() or faces.job.running():
        return
    photos = exif.photos_to_check_for_faces()
    # with multiple workers, the worker's own lock lets only one of these run
    if photos:
        faces.job.start(photos, exif.FACES_CSV)
    faces.set_last_scan_checked(result['finished'])


# on the event loop rather than a thread of its own, like everything else using the database
async def check_photos_after_scans():
    while True:
        await asyncio.sleep(SCAN_RESULT_POLL_SECS)
        try:
            check_photos_after_scan()
        except Exception as e:
            logger.warning(f"Can't start checking new photos for faces: {e}")
        try:
            duplicates.check()
        except Exception as e:
            logger.warning(f"Can't check for duplicates: {e}")
        started = False
        try:
            started = find_new_people()
        except Exception as e:
            logger.warning(f"Can't start finding people: {e}")
        try:
            # not straight after people, whose worker may not have its lock yet
            if not started:
                find_new_dogs()
        except Exception as e:
            logger.warning(f"Can't start finding dogs: {e}")
        try:
            if not started:
                await warm_up_people()
        except Exception as e:
            logger.warning(f"Can't get the people page ready: {e}")


_people_checked = {}
_people_warmed = {}


def _finished(progress):
    return progress.get('finished') if progress else None


async def warm_up_people():
    """
    Works out who groups look like and the map of them once a scan and the jobs after it are done,
    and once after starting, so that /people doesn't wait a minute or two for them.  They're worked
    out again as names change, but only when someone looks
    """
    busy = scans.running() or faces.job.running() or people.job.running() or people.dogs_job.running()
    if busy or not people.available():
        return
    key = (_finished(scans.last_result()), _finished(people.job.progress()), _finished(people.dogs_job.progress()))
    if _people_warmed.get('key') == key:
        return

    def work():
        update_hidden_faces()
        return people.warm_up()
    # the summary takes a few seconds, which would hold up every page if it ran here
    if await asyncio.to_thread(work):
        _people_warmed['key'] = key


def find_new_people():
    """
    Finds the faces in photos the check for faces has just found some in, and says whether it started.
    Once the library has been gone through, that is: the first time takes days here, so it's only
    started on request
    """
    busy = scans.running() or faces.job.running() or people.job.running() or people.dogs_job.running()
    if busy or not people.has_scanned():
        return False
    stamp = exif.faces_stamp()
    if _people_checked.get('stamp') != stamp:
        _people_checked['stamp'] = stamp
        photos = people.to_scan(exif.photos_with_faces())
        if photos:
            return people.start(photos)
    return False


_dogs_checked = {}


def find_new_dogs():
    """
    Finds the dogs in photos that haven't been looked at for them, after the faces in them have been
    found, as both want the processor.  Like finding people, only once the library has been gone
    through, which was done for this one on a faster computer
    """
    busy = scans.running() or faces.job.running() or people.job.running() or people.dogs_job.running()
    if busy or not people.has_looked_for_dogs():
        return
    stamp, _ = exif.shown_photos()
    if _dogs_checked.get('stamp') != stamp:
        _dogs_checked['stamp'] = stamp
        photos = people.to_look_for_dogs(exif.shown_photo_list())
        if photos:
            people.look_for_dogs(photos)


def update_hidden_faces():
    stamp, shown = exif.shown_photos()
    people.set_hidden_photos(stamp, shown)


class FaceNames(BaseModel):
    face_ids: list[str]
    # blank for faces that aren't anyone worth naming
    name: str = Field(max_length=80)
    # moved because they seemed to be someone else, so not to be asked about again
    checked: bool = False


class FaceNamesRestore(BaseModel):
    previous: dict[str, str | None]
    checked: bool = False


class FaceIds(BaseModel):
    face_ids: list[str]


class FacesChecked(BaseModel):
    face_ids: list[str]
    checked: bool


class FacesNotPerson(BaseModel):
    face_ids: list[str]
    name: str
    not_person: bool


class PersonRename(BaseModel):
    name: str
    new_name: str = Field(max_length=80)


class PersonName(BaseModel):
    name: str


class PersonCover(BaseModel):
    name: str
    # or null to go back to choosing one automatically
    face_id: str | None


@app.get("/people")
async def _people(request: Request, tab: Literal['suggested', 'groups', 'map'] = None, sort: Literal['biggest', 'tightest'] = 'biggest'):
    update_hidden_faces()
    tab = tab or 'map'
    view = {'suggested': people.SUGGESTED, 'groups': sort, 'map': people.MAP}[tab]
    overview = people.overview(view)
    return templates.TemplateResponse(
        request=request,
        name="people.html",
        context={
            **overview,
            "tab": tab,
            "sort": sort,
            "tight_days": people.TIGHT_DAYS,
            "next_groups": more_groups_url(view, overview['groups']) if overview['more_groups'] else None,
            "known_names": people.known_names(),
            "status": people.job.status()
        }
    )


def faces_by_year(faces):
    # which year each (face, photo)'s photo is from, going by the same dates as the person's photos
    taken = {photo['id']: photo_year(photo) for photo in exif.photos_with_ids(list({photo for _, photo in faces}))}
    by_year = {}
    for face, photo in faces:
        if photo in taken:
            by_year.setdefault(taken[photo], []).append(face)
    return by_year


def years_newest_first(by_year):
    years = sorted((key for key in by_year if key != UNDATED_YEAR), reverse=True)
    return years + [UNDATED_YEAR] if UNDATED_YEAR in by_year else years


def people_faces_page(request, title, faces, url, year=None, photos_url=None, name=None, group=None, page=1, comparing=None, show_map=False):
    by_year = faces_by_year(faces)
    # a year whose faces have all gone by now leaves all of them
    shown_year = year if year in by_year and not comparing else None
    face_ids = by_year[shown_year] if shown_year else [face for face, _ in faces]
    year_query = {'year': shown_year} if shown_year else {}
    comparison = None
    if comparing == people.DOUBTS:
        comparison = people.doubtful_faces(name)
    elif comparing == people.LIKE and not show_map:
        comparison = people.faces_like(name)
    rows = {}
    if comparison:
        rows = {face['id']: face['row'] for face in comparison['faces']}
        face_ids = list(rows)
    # the map fetches what it shows itself
    details = [] if show_map else people.face_details(face_ids[(page - 1) * people.PAGE_FACES:page * people.PAGE_FACES])
    sections = {}
    for face in details if comparison else []:
        if comparing == people.LIKE:
            face['called'] = rows[face['id']]
        sections.setdefault(rows[face['id']], []).append(face)
    rail = [
        {'label': 'All', 'faces': len(faces), 'url': url, 'current': not shown_year and not comparing},
        *([
            {'label': 'May be someone else', 'faces': None, 'url': f"{url}?check=1", 'current': comparing == people.DOUBTS},
            {'label': f"Looks like {name}", 'faces': None, 'url': f"{url}?similar=1", 'current': comparing == people.LIKE}
        ] if name else []),
        *(
            {'label': key, 'faces': len(by_year[key]), 'url': f"{url}?{urlencode({'year': key})}", 'current': key == shown_year}
            for key in years_newest_first(by_year)
        )
    ]
    working = bool(comparison and comparison['working'])
    return templates.TemplateResponse(
        request=request,
        name="people_faces.html",
        context={
            "title": title,
            "faces": details,
            "sections": [
                {
                    'heading': f"Look like {key}" if comparing == people.DOUBTS else f"Called {key}" if key else "Not named yet",
                    # what each of the row's two buttons says they are: stays is '' for no name
                    'move_to': key if comparing == people.DOUBTS else name,
                    'stays': name if comparing == people.DOUBTS else key or '',
                    'faces': value
                }
                for key, value in sections.items()
            ],
            "comparing": comparing,
            "show_map": show_map,
            "similar_url": f"{url}?similar=1",
            "working": working,
            "failed": bool(comparison and comparison['failed']),
            "total": len(faces) if working else len(face_ids),
            "photos_url": f"{photos_url}?{urlencode(year_query)}" if photos_url and shown_year else photos_url,
            "name": name,
            "group": group,
            "years_rail": rail if name or len(by_year) > 1 else [],
            "shown_year": shown_year,
            # a group's are only ever the best ones, with the rest coming up as those get named
            "next_url": f"{url}?{urlencode({**year_query, 'page': page + 1})}" if name and not comparing and page * people.PAGE_FACES < len(face_ids) else None,
            "cover": people.chosen_cover(name) if name else None,
            "known_names": people.known_names()
        }
    )


def more_groups_url(view, groups):
    # where to carry on from the last of these
    last = groups[-1]
    return f"/api/people/groups?{urlencode({'view': view, 'score': last['score'], 'faces': last['faces'], 'after': last['id']})}"


@app.get("/api/people/groups")
async def _more_groups(request: Request, view: Literal['suggested', 'biggest', 'tightest'], score: float, faces: int, after: int):
    update_hidden_faces()
    groups, more = people.groups_after(view, score, faces, after)
    return templates.TemplateResponse(
        request=request,
        name="_person_groups.html",
        context={"groups": groups, "next_groups": more_groups_url(view, groups) if more else None}
    )


@app.get("/api/people/map")
async def _people_map(request: Request, near: str = None):
    update_hidden_faces()
    found = json.dumps(people.cluster_map(near), separators=(',', ':')).encode()
    # about 430KB of numbers for a thousand dots, which squeeze to about a quarter
    if 'gzip' not in request.headers.get('accept-encoding', ''):
        return Response(found, media_type='application/json')
    return Response(gzip.compress(found, 6), media_type='application/json', headers={'Content-Encoding': 'gzip', 'Vary': 'Accept-Encoding'})


class PersonFamily(BaseModel):
    name: str
    family: bool


@app.post("/api/people/family")
async def _set_family(choice: PersonFamily):
    people.set_family(choice.name, choice.family)
    return "saved"


class PersonParent(BaseModel):
    name: str
    slot: Literal['mom', 'dad']
    # or null or blank for nobody
    parent: str | None


@app.post("/api/people/parent")
async def _set_parent(choice: PersonParent):
    parent = clean_name(choice.parent or '') or None
    if parent == choice.name:
        raise HTTPException(status_code=422, detail="Someone can't be their own parent")
    if parent is not None and parent not in people.known_names():
        raise HTTPException(status_code=404, detail=f"There's nobody called {parent}")
    previous = people.set_parent(choice.name, choice.slot, parent)
    return {'previous': previous, 'family': {'parents': people.parents_of(choice.name), **people.relatives(choice.name)}}


@app.get("/people/group/{group_id}")
async def _people_group(request: Request, group_id: int, year: str = None):
    update_hidden_faces()
    faces = people.group_faces(group_id)
    # groups change as faces in them are named
    if not faces:
        return RedirectResponse(url='/people')
    return people_faces_page(request, "Who is this?", faces, f"/people/group/{group_id}", year, group=group_id)


@app.get("/people/group/{group_id}/ids")
async def _people_group_ids(group_id: int, year: str = None):
    update_hidden_faces()
    faces = people.group_faces(group_id)
    # a year that has emptied must not turn into the whole group, as these are about to be named
    face_ids = faces_by_year(faces).get(year) if year else [face for face, _ in faces]
    if not face_ids:
        raise HTTPException(status_code=404, detail="That group has changed")
    return face_ids


@app.get("/people/{name}/faces")
async def _person_faces(request: Request, name: str, year: str = None, page: int = 1, check: bool = False, similar: bool = False, map: bool = False):
    update_hidden_faces()
    faces = people.person_faces(name)
    if not faces:
        return RedirectResponse(url='/people')
    return people_faces_page(
        request,
        name,
        faces,
        f"/people/{quote(name)}/faces",
        year,
        photos_url=f"/people/{quote(name)}",
        name=name,
        page=max(page, 1),
        comparing=people.DOUBTS if check else people.LIKE if similar else None,
        show_map=similar and map
    )


def photos_by_year_page(request, title, photos, url, year, **context):
    """
    A page of photos from anywhere in the library, one year at a time, as a person can be in tens of
    thousands of them
    """
    by_year = {}
    for photo in photos:
        by_year.setdefault(photo_year(photo), []).append(photo)
    years = years_newest_first(by_year)
    shown_year = year if year in by_year else years[0] if years else None
    year_url = lambda key: f"{url}{'&' if '?' in url else '?'}year={key}"
    following = years[years.index(shown_year) + 1:] if shown_year else []
    return templates.TemplateResponse(
        request=request,
        name="library_month.html",
        context={
            "title": title,
            "dated": True,
            "month": None,
            "keeps_redated_photos": True,
            "with_faces": False,
            "faces_checked": 0,
            "years_rail": [
                {'year': key, 'photos': len(by_year[key]), 'url': year_url(key)}
                for key in years
            ],
            "shown_year": shown_year,
            "next_url": year_url(following[0]) if following else None,
            "known_names": people.known_names(),
            "today": datetime.date.today().isoformat(),
            "earliest": f"{exif.LIBRARY_EARLIEST_YEAR}-01-01",
            "photos": [
                {**library_photo(photo), 'day': month_heading(photo)}
                for photo in sorted(by_year.get(shown_year, []), key=lambda photo: photo['taken'], reverse=True)
            ],
            "newer_url": None,
            "older_url": None,
            **context
        }
    )


@app.get("/people/{name}")
async def _person(request: Request, name: str, year: str = None):
    update_hidden_faces()
    photos = exif.photos_with_ids(people.person_photo_ids(name))
    if not photos:
        return RedirectResponse(url='/people')
    return photos_by_year_page(
        request,
        name,
        photos,
        f"/people/{quote(name)}",
        year,
        back_url="/people",
        back_label="People",
        faces_url=f"/people/{quote(name)}/faces",
        person=name
    )


@app.get("/places")
async def _places(request: Request, q: str = ''):
    return templates.TemplateResponse(
        request=request,
        name="places.html",
        context={"places": places.search(q), "query": q, **places.coverage()}
    )


@app.get("/api/places")
async def _search_places(q: str = ''):
    return places.search(q)


@app.get("/places/map")
async def _places_map(request: Request, kind: str = None, key: str = None, lat: float = None, lon: float = None):
    focus = None
    if lat is not None and lon is not None:
        focus = [[lat, lon], [lat, lon]]
    elif kind and key:
        focus = places.bounds(kind, key)
    return templates.TemplateResponse(request=request, name="places_map.html", context={"focus": focus})


@app.get("/api/places/points")
async def _map_points():
    return places.points()


@app.get("/api/places/area")
async def _map_area(key: str):
    photos, clips = places.in_area(key)
    return {
        'photos': len(photos),
        'movies': len(clips),
        'shown_photos': [
            {'id': photo['id'], 'path': photo['path'], 'rotation': photo['rotation'], 'year': photo_year(photo)}
            for photo in photos[:places.AREA_PHOTOS]
        ],
        'shown_movies': [{'path': clip['path'], 'title': clip.get('title')} for clip in clips[:places.AREA_MOVIES]]
    }


@app.get("/places/photos")
async def _place_photos(request: Request, kind: str, key: str, year: str = None):
    place = places.describe(kind, key)
    if place is None:
        return RedirectResponse(url='/places')
    paths = places.paths_at(kind, key)
    return photos_by_year_page(
        request,
        place['label'],
        exif.photos_with_paths(paths),
        f"/places/photos?{urlencode({'kind': kind, 'key': key})}",
        year,
        back_url="/places/map" if kind == places.AREA else "/places",
        back_label="Map" if kind == places.AREA else "Places",
        map_url=None if kind == places.AREA or not places.bounds(kind, key) else f"/places/map?{urlencode({'kind': kind, 'key': key})}",
        # newest first, with their own heading, since a movie has no place among a year's photos
        movies=[
            {**clip, 'url': f"/movies?{urlencode({'path': clip['path']})}"}
            for clip in reversed(movies.clips())
            if clip['path'] in paths
        ]
    )


@app.get("/api/info")
async def _photo_info(path: str):
    details = info.photo_info(path)
    if details is None:
        raise HTTPException(status_code=404, detail="That photo isn't in the index")
    # what a page needs to follow a change of date
    shown = library_photo({'taken': details['taken_raw'], 'date_assigned': details['date_assigned']})
    return {**details, 'when': shown['when']}


UNDATED_YEAR = 'Undated'


def photo_year(photo):
    year = (photo['taken'] or '')[:4]
    return year if year.isdigit() and int(year) >= exif.LIBRARY_EARLIEST_YEAR else UNDATED_YEAR


def month_heading(photo):
    # a person's photos can span years, so days would make for a very long page
    try:
        return f"{datetime.datetime.strptime(photo['taken'][:7], '%Y:%m'):%B %Y}"
    except ValueError:
        return 'Undated'


def clean_name(name):
    # a name is part of its page's address, where a slash would mean something else
    return ' '.join(name.split()).replace('/', '-')


@app.post("/api/people/faces")
async def _name_faces(names: FaceNames):
    return {'previous': people.set_names(names.face_ids, clean_name(names.name), names.checked)}


@app.post("/api/people/faces/clear")
async def _clear_face_names(faces_to_clear: FaceIds):
    return {'previous': people.clear_names(faces_to_clear.face_ids)}


@app.post("/api/people/rename")
async def _rename_person(rename: PersonRename):
    new_name = clean_name(rename.new_name)
    if not new_name:
        raise HTTPException(status_code=422, detail="A name can't be blank")
    previous = people.rename(rename.name, new_name)
    if previous is None and rename.name != new_name:
        raise HTTPException(status_code=404, detail="There's nobody by that name")
    return {'name': new_name, 'previous': previous or {}}


@app.post("/api/people/remove")
async def _remove_person(person: PersonName):
    previous = people.remove(person.name)
    if previous is None:
        raise HTTPException(status_code=404, detail="There's nobody by that name")
    return {'previous': previous}


@app.post("/api/people/cover")
async def _set_person_cover(choice: PersonCover):
    previous = people.set_cover(choice.name, choice.face_id)
    if previous is False:
        raise HTTPException(status_code=422, detail="That face isn't this person's")
    return {'previous': previous}


# what the viewer draws boxes from.  A query parameter, since a photo's id can hold anything
@app.get("/api/photo-faces")
async def _photo_faces(photo_id: str):
    return people.photo_faces(photo_id)


class PhotoPeople(BaseModel):
    name: str
    photo_ids: list[str]
    tagged: bool = True


# someone in photos without a face to go on, or taking that back off
@app.post("/api/photo-people")
async def _tag_person(change: PhotoPeople):
    name = clean_name(change.name)
    if not name:
        raise HTTPException(status_code=422, detail="A name can't be blank")
    if not change.tagged:
        return {'name': name, 'previous': people.untag_person(change.photo_ids, name)}
    # hidden ones too, which can be tagged from the Hidden folder
    photos = exif.photos_with_ids(change.photo_ids, hidden=True)
    return {'name': name, 'added': people.tag_person(photos, name)}


class TagName(BaseModel):
    name: str


class TagPhotos(BaseModel):
    name: str
    photo_ids: list[str]
    tagged: bool = True


class TagRename(BaseModel):
    name: str
    new_name: str


class TagRestore(BaseModel):
    # as the change being undone gave it: each tag's photos, or None for one that didn't exist
    previous: list[dict]


def tag_name(name):
    name = clean_name(name)
    if not name:
        raise HTTPException(status_code=422, detail="A tag can't be blank")
    return name


@app.post("/api/tags")
async def _create_tag(tag: TagName):
    name = tag_name(tag.name)
    tags.create(name)
    return {'name': name}


@app.get("/api/photo-tags")
async def _photo_tags(photo_id: str):
    return tags.of_photo(photo_id)


@app.post("/api/tags/photos")
async def _tag_photos(change: TagPhotos):
    name = tag_name(change.name)
    return {'name': name, 'changed': tags.set_photos(name, change.photo_ids, change.tagged)}


@app.post("/api/tags/rename")
async def _rename_tag(rename: TagRename):
    new_name = tag_name(rename.new_name)
    previous = tags.rename(rename.name, new_name)
    if previous is None:
        raise HTTPException(status_code=404, detail="There's no tag by that name")
    return {'name': new_name, 'previous': previous}


@app.post("/api/tags/delete")
async def _delete_tag(tag: TagName):
    previous = tags.delete(tag.name)
    if previous is None:
        raise HTTPException(status_code=404, detail="There's no tag by that name")
    return {'previous': previous}


@app.post("/api/tags/restore")
async def _restore_tags(restore: TagRestore):
    tags.restore(restore.previous)
    return "saved"


@app.post("/api/people/faces/checked")
async def _check_faces(faces_checked: FacesChecked):
    people.set_checked(faces_checked.face_ids, faces_checked.checked)
    return "saved"


@app.post("/api/people/faces/not-person")
async def _faces_not_person(faces_not_person: FacesNotPerson):
    people.set_not_person(faces_not_person.face_ids, faces_not_person.name, faces_not_person.not_person)
    return "saved"


@app.get("/api/people/comparing")
async def _people_comparing(kind: str, name: str):
    return {'working': people.comparison_working(kind, name)}


@app.get("/api/people/status")
async def _people_status():
    return people.status()


@app.post("/api/people/status")
async def _start_finding_people():
    if not people.start(people.to_scan(exif.photos_with_faces())):
        raise HTTPException(status_code=409, detail="Already finding people")
    return people.status()


@app.delete("/api/people/status")
async def _stop_finding_people():
    if not people.job.cancel():
        raise HTTPException(status_code=409, detail="Not finding people")
    return people.status()


@app.post("/api/people/faces/restore")
async def _restore_face_names(restore: FaceNamesRestore):
    people.restore_names(restore.previous, restore.checked)
    return "saved"


# not async: making one is slow, and would otherwise hold up every other request
@app.get("/api/face/{face_id}")
def _face(face_id: str):
    try:
        cached = people.crop(face_id)
    except Exception as e:
        logger.warning(f"Can't make a picture of face {face_id}: {e}")
        cached = None
    if cached is None:
        raise HTTPException(status_code=404, detail="Face not available")
    return FileResponse(cached, media_type='image/jpeg', headers=IMAGE_CACHE_HEADERS)


class DateAssignment(BaseModel):
    photo_ids: list[str]
    date: datetime.date


class DateRestore(BaseModel):
    # what /api/dates said the dates were before: as stored, or '' for the photo's own date
    previous: dict[str, str]


@app.post("/api/dates")
async def _assign_date(assignment: DateAssignment):
    if not valid_photo_date(assignment.date):
        raise HTTPException(
            status_code=422,
            detail=f"The date has to be from {exif.LIBRARY_EARLIEST_YEAR} up to today"
        )
    return {'previous': exif.assign_date(assignment.photo_ids, assignment.date)}


class TakenChange(BaseModel):
    photo_id: str
    taken: datetime.datetime


# one photo's exact date and time, as library_photo describes it
@app.post("/api/photo-date")
async def _set_taken(change: TakenChange):
    if not valid_photo_date(change.taken.date()):
        raise HTTPException(
            status_code=422,
            detail=f"The date has to be from {exif.LIBRARY_EARLIEST_YEAR} up to today"
        )
    previous = exif.set_taken(change.photo_id, change.taken)
    taken = change.taken.strftime('%Y:%m:%d %H:%M:%S')
    return {'previous': previous, **library_photo({'taken': taken, 'date_assigned': False}), 'taken': taken}


class PhotoIds(BaseModel):
    photo_ids: list[str]


# back to the dates in the photos' files
@app.post("/api/dates/own")
async def _own_dates(photos: PhotoIds):
    previous = exif.own_dates(photos.photo_ids)
    # what each changed one's date is now, as a page follows a change of date
    taken = exif.own_taken(list(previous))
    return {
        'previous': previous,
        'photos': {
            photo_id: {'taken': value, 'when': library_photo({'taken': value, 'date_assigned': False})['when']}
            for photo_id, value in taken.items()
        }
    }


class LocationChange(BaseModel):
    photo_ids: list[str]
    # a point, or neither for the photos having none
    lat: float = Field(default=None, ge=-90, le=90)
    lon: float = Field(default=None, ge=-180, le=180)
    # what their files say instead
    own: bool = False


class LocationRestore(BaseModel):
    # as /api/locations gave it
    previous: dict[str, str]


@app.post("/api/locations")
async def _set_locations(change: LocationChange):
    if change.own:
        value = ''
    elif change.lat is not None and change.lon is not None:
        value = places.point_value(change.lat, change.lon)
    else:
        value = exif.NO_LOCATION
    return {'previous': exif.set_locations(change.photo_ids, value)}


@app.post("/api/locations/restore")
async def _restore_locations(restore: LocationRestore):
    exif.restore_locations(restore.previous)
    return "saved"


@app.get("/api/towns")
async def _towns(q: str = ''):
    return await run_in_threadpool(places.search_towns, q) if q.split(',')[0].strip() else []


@app.post("/api/dates/restore")
async def _restore_dates(restore: DateRestore):
    for value in restore.previous.values():
        if value and not re.fullmatch(r'\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}', value):
            raise HTTPException(status_code=422, detail="Not a date from /api/dates")
    exif.restore_dates(restore.previous)
    return "saved"


@app.get("/api/faces")
async def _faces_status():
    return {**faces.job.status(), **exif.face_counts(), 'indexed_photos': exif.count()}


@app.post("/api/faces")
async def _start_faces():
    if not faces.job.start(exif.photos_to_check_for_faces(), exif.FACES_CSV):
        raise HTTPException(status_code=409, detail="Already checking for faces")
    return "started"


@app.delete("/api/faces")
async def _stop_faces():
    if not faces.job.cancel():
        raise HTTPException(status_code=409, detail="Not checking for faces")
    return "stopping"


@app.get("/movies")
async def _movies_page(request: Request):
    return templates.TemplateResponse(request=request, name="movies.html")


@app.get("/api/movies")
async def _movies():
    return movies.clips()


class MovieHidden(BaseModel):
    id: str
    hidden: bool


@app.post("/api/movies/hidden")
async def _set_movie_hidden(change: MovieHidden):
    exif.set_movie_hidden(change.id, change.hidden)
    return "saved"


@app.delete("/api/movie{path:path}")
async def _delete_movie(path: str):
    files = movies.files_of(library_path(path))
    if files is None:
        raise HTTPException(status_code=404)
    # only these files: a photo with the same name as a video isn't part of it, unlike a Live
    # Photo's clip, and a large movie takes minutes to copy to the trash on another volume
    moved = await run_in_threadpool(trash.discard_files, files)
    if not moved:
        raise HTTPException(status_code=404)
    exif.record_deleted(moved)
    return {'files': moved}


@app.get("/api/poster{path:path}")
def _poster(path: str, size: Literal['M', 'XL'] = 'M'):
    return cached_image(lambda video, _: thumbs.video_poster(video, size), path, 0)


# declared after /api/movies, which this would otherwise also match
@app.get("/api/movie{path:path}")
def _movie(path: str, hevc: bool = True, copy: bool = False):
    file = movies.playable_file(library_path(path), hevc, copy)
    if file is None:
        raise HTTPException(status_code=404)
    # browsers play .mov files they've been told are .mp4, which is the same format underneath,
    # and some won't try them otherwise
    return FileResponse(file, media_type='video/mp4', headers=IMAGE_CACHE_HEADERS)


class DuplicateChoice(BaseModel):
    keep: str
    photo_ids: list[str]


class DuplicatesShown(BaseModel):
    photo_ids: list[str]


@app.get("/duplicates")
async def _duplicates_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="duplicates.html",
        context=duplicates.page()
    )


@app.get("/api/duplicates")
async def _duplicates_status():
    return duplicates.status()


@app.post("/api/duplicates")
async def _start_duplicates():
    if not duplicates.start():
        raise HTTPException(status_code=409, detail="Already looking for duplicates")
    return "started"


@app.delete("/api/duplicates")
async def _stop_duplicates():
    if not duplicates.job.cancel():
        raise HTTPException(status_code=409, detail="Not looking for duplicates")
    return "stopping"


@app.post("/api/duplicates/keep")
async def _keep_duplicate(choice: DuplicateChoice):
    duplicates.keep(choice.keep, choice.photo_ids)
    return "saved"


@app.post("/api/duplicates/show")
async def _show_duplicates(shown: DuplicatesShown):
    duplicates.show(shown.photo_ids)
    return "saved"


def slides(df, bursts=None):
    return df.assign(
        date=df['DateTimeOriginal'].map(
            lambda d: d.split(' ')[0].replace(':', '/')
        ),
        burst=df['photo_id'].map(lambda photo_id: (bursts or {}).get(photo_id, []))
    )[[
        'SourceFile', 'date', 'photo_id', 'favorite', 'rotation', 'review_status', 'burst'
    ]].to_dict(orient='records')


def kiosk_photos(min_pics):
    date = datetime.date.today()
    # photos from other dates, recent ones included, would make up the numbers while today's wait
    # unseen, so a date with photos to review shows only its own approved ones until they're decided
    fill_in = not exif.review_counts(date_filter(date, True))[exif.UNREVIEWED]
    recent = slides(*exif.recent_photos(recent_range(KIOSK_RECENT_DAYS), KIOSK_RECENT_PHOTOS)) if fill_in else []
    recent_ids = {pic['photo_id'] for pic in recent}
    pics = []
    offs = 0
    # search up to +/- 10 days for images until we get to the minimum
    # number requested
    while len(pics) < min_pics and offs < (10 if fill_in else 1):
        for sign in (-1, 1) if offs else (1,):
            dt = date + datetime.timedelta(days=offs * sign)
            df = exif.select_photos(f"%{dt.month:02d}:{dt.day:02d} ")
            if len(df):
                pics.extend(slides(sample_across_years(df, len(df))))
        offs += 1
    # a frame should have something to show, unless what's missing is today's to review
    if fill_in and len(pics) < min_pics:
        pics.extend(slides(exif.stand_in_photos(
            [*recent_ids, *(pic['photo_id'] for pic in pics)],
            KIOSK_STAND_INS,
            date.isoformat()
        )))
    others = (pic for pic in pics if pic['photo_id'] not in recent_ids)
    # taking turns, so that neither has to wait for the other, which can be a long list, to finish
    return [pic for pic in itertools.chain(*itertools.zip_longest(recent, others)) if pic]


@app.get("/api/kiosk/photos")
async def _kiosk_photos(min_pics: int = 20):
    return kiosk_photos(min_pics)


@app.get("/kiosk")
async def _kiosk(request: Request, refresh_secs: int = 300, min_pics: int = 20):
    date = datetime.date.today()
    images = kiosk_photos(min_pics)
    return templates.TemplateResponse(
        request=request,
        name="kiosk.html",
        context={
            "refresh_secs": refresh_secs,
            "min_pics": min_pics,
            "review_url": review_url(date, True, kiosk=request.url.query, recent_days=KIOSK_RECENT_DAYS),
            "today": date.isoformat(),
            "to_review": kiosk_to_review(date),
            "photos_version": exif.photos_version(),
            "build": PAGE_BUILD,
            "images": images,
            # behind the link to review, while today has nothing approved to show
            "backgrounds": [] if images else exif.favorite_photos(KIOSK_STAND_INS),
            # what's keeping the frame empty, on a date in any year
            "today_to_review": 0 if images else exif.review_counts(date_filter(date, True))[exif.UNREVIEWED]
        }
    )


@app.get("/")
async def _index(request: Request, response_class=HTMLResponse):
    # relative, since behind the proxy this app sees http and an absolute url would say so
    return RedirectResponse(url='/library')


# browsers and iOS also look for these at the root, without being told where they are
@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
async def _apple_touch_icon():
    return FileResponse(f"{STATIC_DIR}/apple-touch-icon.png")


# served from the root because that decides which pages it can cover: all those the frame can reach
@app.get("/kiosk-sw.js", include_in_schema=False)
async def _kiosk_service_worker():
    return FileResponse(
        f"{STATIC_DIR}/kiosk-sw.js",
        media_type='text/javascript',
        headers={'Cache-Control': 'no-cache'}
    )


@app.get("/favicon.ico", include_in_schema=False)
async def _favicon():
    return FileResponse(f"{STATIC_DIR}/favicon.ico")
