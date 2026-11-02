"""
Downloads the models that find and describe dogs, YOLOX-m and DINOv2-small, both Apache 2.0, and
checks them, since they run as code.

Usage: fetch_dog_models.py <folder>
"""
import hashlib
import os
import sys
import urllib.request

MODELS = {
    'yolox_m.onnx': (
        'https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_m.onnx',
        '21ff6cfdeb53b013bac2249599e55f00bff3cfdfdab37ed7a4620818c1d15b3f'
    ),
    'dinov2_small.onnx': (
        'https://huggingface.co/Xenova/dinov2-small/resolve/c2bb04a51fab207c420665f1946016107bffc701/onnx/model.onnx',
        '83141175ec78b4ff9a2bb58a4c7c264ba0054d1c2e122e5a8114b79a8d4179ea'
    )
}


def main(folder):
    os.makedirs(folder, exist_ok=True)
    for name, (url, sha256) in MODELS.items():
        with urllib.request.urlopen(url) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != sha256:
            sys.exit(f"{name} isn't the model expected")
        with open(os.path.join(folder, name), 'wb') as fh:
            fh.write(data)


if __name__ == '__main__':
    main(*sys.argv[1:])
