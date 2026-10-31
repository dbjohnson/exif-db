import os
import shutil
import datetime
import glob
import logging
from io import BytesIO
from functools import lru_cache
from typing import Literal
from urllib.parse import urlencode

import rawpy
from fastapi import FastAPI, Query, Request, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from api import exif, scans, thumbs


app = FastAPI()
app.mount("/static", StaticFiles(directory="/src/app/static"), name="static")
templates = Jinja2Templates(directory="templates")
exif.load_csv()
scans.start_scheduler()
DEFAULT_NUM_IMAGES = int(os.getenv('DEFAULT_NUM_IMAGES', 20))
ReviewStatus = Literal['unreviewed', 'rejected', 'reviewed']
logger = logging.getLogger('uvicorn.error')
# lets a browser reuse a photo instead of having the NAS read and convert it again
IMAGE_CACHE_HEADERS = {'Cache-Control': 'private, max-age=86400'}


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


def date_filter(date, all_years):
    # the DateTimeOriginal prefix for a date; no date means the whole library
    if date is None:
        return ''
    if all_years:
        return f"%{date.month:02d}:{date.day:02d} "
    return f"{date.year:04d}:{date.month:02d}:{date.day:02d} "


def review_url(date, all_years, status='unreviewed'):
    params = {'status': status}
    if date:
        params.update(date=date.isoformat(), all_years=str(all_years).lower())
    return f"/review?{urlencode(params)}"


def gallery(request, date, all_years, n):
    datestring = date_filter(date, all_years)
    counts = exif.review_counts(datestring)
    pics = exif.select_photos(datestring)
    if len(pics) > n:
        pics = pics.sample(n=n)

    return templates.TemplateResponse(
        request=request,
        name="gallery.html",
        context={
            "picked_date": date.isoformat() if date else None,
            "all_years": all_years,
            "counts": counts,
            "background": None if counts[exif.APPROVED] else exif.background_photo(),
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
async def _set_scan_schedule(hours: int):
    if hours < 0:
        raise HTTPException(status_code=422, detail="hours can't be negative")
    scans.set_schedule_hours(hours)
    return scans.next_scan()


@app.get("/manage")
async def _manage(request: Request):
    return templates.TemplateResponse(request=request, name="manage.html")


@app.delete("/api/image{path:path}")
async def _delete_image(path: str, background_tasks: BackgroundTasks):
    # a scan in progress could add back the row for a file deleted now
    if scans.running():
        raise HTTPException(status_code=409, detail="Can't delete images while a scan is running")

    def delete():
        # assume any files differing only by extension should also be deleted
        files = glob.glob(f"{glob.escape(os.path.splitext(path)[0])}.*")
        exif.delete_images({path, *files})
        # move files to deleted directory (user can then permanently delete),
        # keeping their paths since file names aren't unique across the library
        for f in files:
            destination = os.path.join('/storage/deleted', f.lstrip('/'))
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.move(f, destination)

    background_tasks.add_task(delete)

# not concerned about cross worker caching, etc.
@lru_cache(maxsize=1000)
@app.get("/api/image{path:path}")
async def _image(request: Request, path: str):
    extension = path.split('.')[-1].upper()
    if extension == 'HEIC':
        # HEIC images need to be converted to JPEG or browsers won't display them
        im = thumbs.read_heic(path)
        # resize to max 2000px
        width, height = im.size
        ratio = int(os.getenv('MAX_IMAGE_SIZE', 2000)) / max(im.size)
        if ratio < 1:
            im = im.resize((int(width * ratio), int(height * ratio)))
        buffered = BytesIO()
        im.save(buffered, format="JPEG")
        img_bytes = buffered.getvalue()
        return Response(content=img_bytes, headers=IMAGE_CACHE_HEADERS)
    elif extension in thumbs.RAW_EXTENSIONS:
        # extract thumnails for raw
        with rawpy.imread(path) as raw:
            img_bytes = raw.extract_thumb().data
        return Response(content=img_bytes, headers=IMAGE_CACHE_HEADERS)
    else:
        with open(path, 'rb') as fh:
            img_bytes = fh.read()
        return Response(content=img_bytes, headers=IMAGE_CACHE_HEADERS)


# not async: decoding is slow, and would otherwise hold up every other request
@app.get("/api/thumb{path:path}")
def _thumb(path: str):
    if not os.path.normpath(path).startswith(f"{exif.LIBRARY_ROOT}/"):
        raise HTTPException(status_code=404)
    try:
        thumbnail = thumbs.thumbnail(path)
    except Exception as e:
        # decoders each raise their own errors; the review grid shows a placeholder instead
        logger.warning(f"No thumbnail for {path}: {e}")
        raise HTTPException(status_code=404, detail="No thumbnail available")
    return FileResponse(
        thumbnail,
        media_type='image/jpeg',
        headers={'Cache-Control': 'private, max-age=86400'}
    )


@app.get("/api/tags")
async def _tags():
    return exif.tags()


@app.get("/api/exif")
async def _exif(source_file: str):
    return exif.db.execute(
        f"SELECT * FROM exif WHERE SourceFile = '{source_file}'"
    ).fetchdf().fillna('').to_dict(orient='records')[0]


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
    status: ReviewStatus = 'unreviewed'
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
    return templates.TemplateResponse(
        request=request,
        name="review.html",
        context={
            "scope": scope,
            "status": status,
            "done_url": done_url,
            "query": {
                'status': status,
                'all_years': all_years,
                **({'date': date.isoformat()} if date else {})
            }
        }
    )


@app.get("/kiosk")
async def _kiosk(request: Request, refresh_secs: int = 300, min_pics: int = 20):
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
                df = df.assign(
                    date=df['DateTimeOriginal'].map(
                        lambda d: d.split(' ')[0].replace(':', '/')
                    )
                )[[
                    'SourceFile', 'date', 'photo_id', 'favorite'
                ]]
                pics.extend(
                    df.to_dict(orient='records')
                )
        offs += 1

    return templates.TemplateResponse(
        request=request,
        name="kiosk.html",
        context={
            "refresh_secs": refresh_secs,
            "review_url": review_url(date, True),
            "background": None if pics else exif.background_photo(),
            "images": pics
        }
    )


@app.get("/")
async def _index(request: Request, response_class=HTMLResponse):
    return RedirectResponse(url=request.url_for('_random'))
