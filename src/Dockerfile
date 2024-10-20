FROM python:3.13-slim-bookworm

RUN apt-get update && apt-get install -y libimage-exiftool-perl
RUN pip3 install "fastapi[standard]" uvicorn duckdb pandas pillow pillow-heif ipython
