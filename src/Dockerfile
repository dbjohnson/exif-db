FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y libimage-exiftool-perl
RUN pip3 install "fastapi[standard]" uvicorn jinja2 duckdb pandas pillow pyheif rawpy ipython

# copy over the app
WORKDIR /src/app
COPY . /src/app/

