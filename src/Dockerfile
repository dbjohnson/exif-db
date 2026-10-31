FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y libimage-exiftool-perl
# duckdb 2.x needs AVX, and crashes on startup on CPUs without it (like the NAS's Celeron J4125)
RUN pip3 install "fastapi[standard]" uvicorn jinja2 duckdb==1.5.6 pandas pillow pyheif rawpy ipython

# copy over the app
WORKDIR /src/app
COPY . /src/app/

