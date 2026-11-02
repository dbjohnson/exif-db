FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y libimage-exiftool-perl
# duckdb 2.x needs AVX, and crashes on startup on CPUs without it (like the NAS's Celeron J4125)
RUN pip3 install "fastapi[standard]" uvicorn jinja2 duckdb==1.5.6 pandas pillow pyheif rawpy ipython opencv-python-headless==5.0.0.93 onnxruntime==1.31.1 iso3166==2.1.1

COPY scripts/fetch_face_models.py /tmp/fetch_face_models.py
RUN python3 /tmp/fetch_face_models.py /opt/face-models
COPY scripts/fetch_dog_models.py /tmp/fetch_dog_models.py
RUN python3 /tmp/fetch_dog_models.py /opt/dog-models
COPY scripts/fetch_places.py /tmp/fetch_places.py
RUN python3 /tmp/fetch_places.py /opt/places

# copy over the app
WORKDIR /src/app
COPY . /src/app/

