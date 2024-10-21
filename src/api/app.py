import os
import shutil
import datetime
import base64
import random
from io import BytesIO

import rawpy
import pyheif
from PIL import Image
from jinja2 import Environment, FileSystemLoader
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from api import exif


app = FastAPI()
app.mount("/photo", StaticFiles(directory="/photo"), name="photo")

exif.load_csv()

# Jinja template
template = Environment(
    loader=FileSystemLoader("./templates")
).get_template("gallery.html")


@app.get("/api/ping")
async def _ping():
    return "pong"


@app.get("/api/reload")
async def _reload():
    try:
        exif.load_csv()

        return f"Last modified: {exif.last_modified()}"
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/images")
async def _images(path: str, request: Request):
    try:
        exif.delete_image(path)
        # move file to deleted directory (user can then permanently delete)
        deleted_dir = '/storage/deleted'
        os.makedirs(deleted_dir, exist_ok=True)
        shutil.move(path, os.path.join(deleted_dir, os.path.basename(path)))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tags")
async def _tags():
    try:
        return exif.tags()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/exif")
async def _exif(source_file: str):
    try:
        return exif.db.execute(
            f"SELECT * FROM exif WHERE SourceFile = '{source_file}'"
        ).fetchdf().fillna('').to_dict(orient='records')[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
    if False:
        try:
            return exif.dataframe().fillna('').to_csv(index=False)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/onthisday")
async def _onthisday(month: int = -1, day: int = -1, n: int = 10, response_class=HTMLResponse):
    try:
        if min(month, day) > 0:
            date = datetime.date(month=month, day=day, year=datetime.date.today().year)
        else:
            date = datetime.date.today()
        pics = exif.select_by_date(f"%{date.month:02d}:{date.day:02d} ").to_dict(orient='records')
        if len(pics) > n:
            random.shuffle(pics)
            pics = pics[:n]

        if len(pics) == 0:
            return f"No pictures found for {date}"
        else:
            return HTMLResponse(_html_for_pics(pics))

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/random")
async def _random(n: int = 10, response_class=HTMLResponse):
    try:
        pics = exif.dataframe()
        if len(pics) > n:
            pics = pics.sample(n=n)
        return HTMLResponse(_html_for_pics(pics.to_dict(orient='records')))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _html_for_pics(pics):
    def load_image(pic):
        path = pic["SourceFile"]
        extension = pic["FileType"]
        if extension == 'HEIC':
            # HEIC images need to be converted to JPEG or browsers won't display them
            heif_file = pyheif.read(pic['SourceFile'])
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
            return f"data:image/png;base64, {base64.b64encode(img_bytes).decode()}"
        elif extension in ('ARW', 'CR2', "NEF"):
            # extract thumnails for raw
            with rawpy.imread(path) as raw:
                img_bytes = raw.extract_thumb().data
            return f"data:image/png;base64, {base64.b64encode(img_bytes).decode()}"
        else:
            return path

    return template.render(
        images=[{
            'path': pic['SourceFile'],
            'src': load_image(pic),
            'date': pic['DateTimeOriginal'].split(' ')[0].replace(':', '/'),
        }
            for pic in sorted(pics, key=lambda x: x['DateTimeOriginal'], reverse=True)
        ]
    )

    return
