"""
Downloads GeoNames' list of towns and cities (CC BY 4.0), packaged by reverse_geocoder, for working
out where a photo was taken without asking anyone else.

Usage: fetch_places.py <folder>
"""
import hashlib
import io
import os
import sys
import tarfile
import urllib.request

URL = 'https://files.pythonhosted.org/packages/0b/0f/b7d5d4b36553731f11983e19e1813a1059ad0732c5162c01b3220c927d31/reverse_geocoder-1.5.1.tar.gz'
SHA256 = '2a2e781b5f69376d922b78fe8978f1350c84fce0ddb07e02c834ecf98b57c75c'
MEMBER = 'rg_cities1000.csv'


def main(folder):
    with urllib.request.urlopen(URL) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != SHA256:
        sys.exit("The list of places isn't the one expected")
    os.makedirs(folder, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        member = next(item for item in archive.getmembers() if item.name.endswith(MEMBER))
        with open(os.path.join(folder, 'cities.csv'), 'wb') as fh:
            fh.write(archive.extractfile(member).read())


if __name__ == '__main__':
    main(*sys.argv[1:])
