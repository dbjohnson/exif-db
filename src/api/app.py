import os
import datetime
import hashlib
import itertools
import logging
import re
from typing import Literal
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, Query, Request, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from api import exif, faces, scans, thumbs, trash


app = FastAPI()
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(APP_DIR, "static")
TEMPLATES_DIR = os.path.join(APP_DIR, "templates")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)
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


def review_url(date, all_years, status='unreviewed', kiosk=None):
    params = {'status': status}
    if date:
        params.update(date=date.isoformat(), all_years=str(all_years).lower())
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
        pics = sample_across_years(pics, n) if date else pics.sample(n=n)

    return templates.TemplateResponse(
        request=request,
        name="gallery.html",
        context={
            "picked_date": date.isoformat() if date else None,
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
    return {**scans.status(), 'indexed_photos': exif.count(), 'reviews': exif.review_counts()}


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
def _delete_image(path: str):
    path = library_path(path)
    files = trash.discard(path)
    exif.record_deleted({path, *files})
    return {'files': files}


@app.post("/api/restore")
def _restore_images(request: RestoreRequest):
    restored = trash.restore([library_path(f) for f in request.files])
    if not restored:
        raise HTTPException(status_code=404, detail="Nothing to restore")
    exif.record_restored(restored)
    return {'files': restored}


@app.get("/api/trash")
def _trash():
    return trash.summary()


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


@app.get("/random")
async def _random(request: Request, n: int = DEFAULT_NUM_IMAGES):
    return gallery(request, None, True, n)


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
    limit: int = Query(default=24, ge=1, le=200)
):
    return exif.review_page(date_filter(date, all_years), status, offset, limit)


@app.get("/api/review/today")
async def _review_today():
    date = datetime.date.today()
    return {
        'date': date.isoformat(),
        'unreviewed': exif.review_counts(date_filter(date, True))[exif.UNREVIEWED],
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
    kiosk: str = None
):
    if date is None:
        scope = "All dates"
        done_url = "/random" if status == 'unreviewed' else "/manage"
    else:
        scope = f"{date:%B} {date.day}" + ("" if all_years else f", {date.year}")
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
                **({'date': date.isoformat()} if date else {})
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
        return sorted((undated_photo(photo, dated_folders) for photo in photos), key=lambda photo: photo['day'])
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
            "library_url": f"/library{library_query(with_faces)}",
            "other_filter_url": library_url(month, not with_faces),
            "month": month.replace(':', '-') if month else None,
            "today": datetime.date.today().isoformat(),
            "earliest": f"{exif.LIBRARY_EARLIEST_YEAR}-01-01",
            "photos": library_month_photos(month, with_faces),
            "newer_url": library_url(months[position - 1], with_faces) if position else None,
            "older_url": library_url(months[position + 1], with_faces) if position + 1 < len(months) else None
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
            "faces_checked": exif.face_counts()['checked'],
            "other_filter_url": f"/library{library_query(not with_faces)}",
            "years": library_years(tiles),
            "undated": next((tile for tile in tiles if not tile['year']), None)
        }
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


@app.post("/api/dates/restore")
async def _restore_dates(restore: DateRestore):
    for value in restore.previous.values():
        if value and not re.fullmatch(r'\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}', value):
            raise HTTPException(status_code=422, detail="Not a date from /api/dates")
    exif.restore_dates(restore.previous)
    return "saved"


@app.get("/api/faces")
async def _faces_status():
    return {**faces.status(), **exif.face_counts(), 'indexed_photos': exif.count()}


@app.post("/api/faces")
async def _start_faces():
    if not faces.start(exif.photos_to_check_for_faces(), exif.FACES_CSV):
        raise HTTPException(status_code=409, detail="Already checking for faces")
    return "started"


@app.delete("/api/faces")
async def _stop_faces():
    if not faces.cancel():
        raise HTTPException(status_code=409, detail="Not checking for faces")
    return "stopping"


def slides(df):
    return df.assign(
        date=df['DateTimeOriginal'].map(
            lambda d: d.split(' ')[0].replace(':', '/')
        )
    )[[
        'SourceFile', 'date', 'photo_id', 'favorite', 'rotation'
    ]].to_dict(orient='records')


def kiosk_photos(min_pics):
    date = datetime.date.today()
    pics = []
    offs = 0
    # search up to +/- 10 days for images until we get to the minimum
    # number requested
    while len(pics) < min_pics and offs < 10:
        for sign in (-1, 1) if offs else (1,):
            dt = date + datetime.timedelta(days=offs * sign)
            df = exif.select_photos(f"%{dt.month:02d}:{dt.day:02d} ")
            if len(df):
                pics.extend(slides(sample_across_years(df, len(df))))
        offs += 1
    # a frame should always have something to show
    if len(pics) < min_pics:
        pics.extend(slides(exif.stand_in_photos(
            [pic['photo_id'] for pic in pics],
            KIOSK_STAND_INS,
            date.isoformat()
        )))
    return pics


@app.get("/api/kiosk/photos")
async def _kiosk_photos(min_pics: int = 20):
    return kiosk_photos(min_pics)


@app.get("/kiosk")
async def _kiosk(request: Request, refresh_secs: int = 300, min_pics: int = 20):
    date = datetime.date.today()
    return templates.TemplateResponse(
        request=request,
        name="kiosk.html",
        context={
            "refresh_secs": refresh_secs,
            "min_pics": min_pics,
            "review_url": review_url(date, True, kiosk=request.url.query),
            "today": date.isoformat(),
            "to_review": exif.review_counts(date_filter(date, True))[exif.UNREVIEWED],
            "photos_version": exif.photos_version(),
            "build": PAGE_BUILD,
            "images": kiosk_photos(min_pics)
        }
    )


@app.get("/")
async def _index(request: Request, response_class=HTMLResponse):
    # relative, since behind the proxy this app sees http and an absolute url would say so
    return RedirectResponse(url='/random')


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
