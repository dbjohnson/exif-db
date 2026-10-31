import os
import datetime
import hashlib
import logging
from typing import Literal
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, Query, Request, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from api import exif, scans, thumbs, trash


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


class FavoriteChange(BaseModel):
    photo_id: str
    favorite: bool


class RotationChange(BaseModel):
    photo_id: str
    rotation: Rotation


@app.post("/api/rotate")
async def _set_rotation(change: RotationChange):
    exif.set_rotation(change.photo_id, change.rotation)
    return "saved"


@app.post("/api/favorite")
async def _set_favorite(change: FavoriteChange):
    exif.set_favorite(change.photo_id, change.favorite)
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
    exif.record_reviews(decisions.approved, decisions.rejected)
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
