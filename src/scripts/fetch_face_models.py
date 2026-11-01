"""
Downloads InsightFace's buffalo_l face models, which are for non-commercial use, and checks them
before unpacking the two this uses, since they run as code.

Usage: fetch_face_models.py <folder>
"""
import hashlib
import io
import os
import sys
import urllib.request
import zipfile

URL = 'https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip'
SHA256 = '80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f'
NAMES = ('det_10g.onnx', 'w600k_r50.onnx')


def main(folder):
    with urllib.request.urlopen(URL) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != SHA256:
        sys.exit("The models aren't the ones expected")
    os.makedirs(folder, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in NAMES:
            with open(os.path.join(folder, name), 'wb') as fh:
                fh.write(archive.read(name))


if __name__ == '__main__':
    main(*sys.argv[1:])
