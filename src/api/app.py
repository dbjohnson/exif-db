import os
import shutil
import datetime
import glob
from io import BytesIO
from functools import lru_cache

import rawpy
import pyheif
from PIL import Image
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import StreamingResponse, HTMLResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from api import exif


app = FastAPI()
app.mount("/static", StaticFiles(directory="/src/app/static"), name="static")
templates = Jinja2Templates(directory="templates")
exif.load_csv()
DEFAULT_NUM_IMAGES = int(os.getenv('DEFAULT_NUM_IMAGES', 20))


@app.get("/api/ping")
async def _ping():
    return "pong"


@app.get("/api/reload")
async def _reload():
    exif.load_csv()
    return f"Last modified: {exif.last_modified()}"


@app.delete("/api/image{path:path}")
async def _delete_image(path: str, background_tasks: BackgroundTasks):
    def delete():
        exif.delete_image(path)
        # move file to deleted directory (user can then permanently delete)
        deleted_dir = '/storage/deleted'
        os.makedirs(deleted_dir, exist_ok=True)
        # assume any files differing only by extension should also be deleted
        for f in glob.glob(f"{os.path.splitext(path)[0]}.*"):
            shutil.move(f, os.path.join(deleted_dir, os.path.basename(f)))

    background_tasks.add_task(delete)

# not concerned about cross worker caching, etc.
@lru_cache(maxsize=1000)
@app.get("/api/image{path:path}")
async def _image(request: Request, path: str):
    extension = path.split('.')[-1].upper()
    if extension == 'HEIC':
        # HEIC images need to be converted to JPEG or browsers won't display them
        heif_file = pyheif.read(path)
        im = Image.frombytes(
            heif_file.mode,
            heif_file.size,
            heif_file.data,
            "raw",
            heif_file.mode,
            heif_file.stride
        )
        # resize to max 2000px
        width, height = im.size
        ratio = int(os.getenv('MAX_IMAGE_SIZE', 2000)) / max(im.size)
        if ratio < 1:
            im = im.resize((int(width * ratio), int(height * ratio)))
        buffered = BytesIO()
        im.save(buffered, format="JPEG")
        img_bytes = buffered.getvalue()
        return Response(content=img_bytes)
    elif extension in ('ARW', 'CR2', "NEF"):
        # extract thumnails for raw
        with rawpy.imread(path) as raw:
            img_bytes = raw.extract_thumb().data
        return Response(content=img_bytes)
    else:
        with open(path, 'rb') as fh:
            img_bytes = fh.read()
        return Response(content=img_bytes)


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
async def _onthisday(request: Request, month: int = -1, day: int = -1, n: int = DEFAULT_NUM_IMAGES):
    if min(month, day) > 0:
        today = datetime.date.today()
        date = datetime.date(month=month, day=day, year=today.year)
    else:
        date = datetime.date.today()
    pics = exif.select_by_date(f"%{date.month:02d}:{date.day:02d} ")
    if len(pics) > n:
        pics = pics.sample(n=n)

    return templates.TemplateResponse(
        request=request,
        name="gallery.html",
        context={
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


@app.get("/random")
async def _random(request: Request, n: int = DEFAULT_NUM_IMAGES):
    pics = exif.dataframe()
    if len(pics) > n:
        pics = pics.sample(n=n)

    return templates.TemplateResponse(
        request=request,
        name="gallery.html",
        context={
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
            df = exif.select_by_date(f"%{dt.month:02d}:{dt.day:02d} ")
            if len(df):
                df = df.assign(
                    date=df['DateTimeOriginal'].map(
                        lambda d: d.split(' ')[0].replace(':', '/')
                    )
                )[[
                    'SourceFile', 'date'
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
            "images": pics
        }
    )


@app.get("/")
async def _index(request: Request, response_class=HTMLResponse):
    return RedirectResponse(url=request.url_for('_random'))
