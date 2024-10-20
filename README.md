# exif-db
Exif database and API

This codebase contains scripts to collect exif data and expose via database and API interfaces.  It's a light wrapper around the excellent [exiftool](https://exiftool.org/), which does all the heavy lifting.

## Usage

Modify [compose.prod.yml](./src/compose.prod.yaml) to mount your media directory and a volume for storage


Scan all files for exif data; by default subsequent runs will only check files created or modified since the last run
```bash
docker compose run --rm -d bash -c "/src/scripts/scan.sh"
```

Start webservice / API
```bash
docker compose up -d
```
