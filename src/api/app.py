import datetime
import base64
from io import BytesIO

from PIL import Image
from pillow_heif import register_heif_opener
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import exif


app = FastAPI()
app.mount("/photo", StaticFiles(directory="/photo"), name="photo")

register_heif_opener()
exif.load_csv()


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
async def _onthisday(month: int = -1, day: int = -1, response_class=HTMLResponse):
    try:
        if min(month, day) > 0:
            date = datetime.date(month=month, day=day, year=datetime.date.today().year)
        else:
            date = datetime.date.today()
        pics = exif.select_by_date(f"%{date.month:02d}:{date.day:02d}").to_dict(orient='records')

        if len(pics) == 0:
            return f"No pictures found for {date}"
        else:
            def load_image(pic):
                # HEIC images need to be converted to JPEG or browsers won't display them
                path = pic["SourceFile"]
                if path.lower().endswith(".heic"):
                    im = Image.open(path)
                    buffered = BytesIO()
                    im.save(buffered, format="JPEG")
                    img_str = base64.b64encode(buffered.getvalue())
                    return f"data:image/png;base64, {img_str.decode()}"
                else:
                    return path

            return HTMLResponse(f"""
            <html>
            <body>
            {"\n".join([
                f'<img src="{load_image(pic)}" alt="{pic["DateTimeOriginal"]}" style="height:80%;margin-bottom:5px">'
                for pic in pics
            ])}
            </div>
            </body>
            </html>
            """)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
