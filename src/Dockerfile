FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y libimage-exiftool-perl
RUN pip3 install "fastapi[standard]" uvicorn duckdb pandas pillow pillow-heif rawpy imageio ipython

# copy over the app
WORKDIR /src/app
COPY . /src/app/

