"""
Puts exports on Cloudflare R2, each under its own name in one bucket, served from the bucket's
r2.dev address.  Everything comes from one API token, which needs only Workers R2 Storage: Edit:
R2's S3 keys are the token's id and the SHA-256 of the token itself.  Used by export_photos.py, so
it imports nothing from the api package.

Set CLOUDFLARE_API_TOKEN, and CLOUDFLARE_ACCOUNT_ID unless the token reaches only one account.
R2_BUCKET names the bucket, which is made if it isn't there.
"""
import hashlib
import json
import mimetypes
import os
import urllib.error
import urllib.request

API = 'https://api.cloudflare.com/client/v4'
DEFAULT_BUCKET = 'exif-db-exports'
# where the bucket's address is kept once worked out, for the app to show
ADDRESS_JSON = '/storage/r2.json'
# the page itself is asked for every time, so an update shows at once; everything it loads is
# named by what's in it or asked for with its version
PAGE_CACHE = 'no-cache'
FILE_CACHE = 'public, max-age=3600'
# big files, such as the zip, go up in parts of this size, at most 10,000 of them
PART_BYTES = 128 * 1024 * 1024
DELETE_BATCH = 1000
# what Python doesn't know of, as files from a phone
mimetypes.add_type('image/heic', '.heic')
mimetypes.add_type('image/heif', '.heif')
mimetypes.add_type('text/javascript', '.js')


class PublishError(Exception):
    pass


def configured():
    return bool(os.getenv('CLOUDFLARE_API_TOKEN'))


def bucket_name():
    return os.getenv('R2_BUCKET') or DEFAULT_BUCKET


def _api(method, path, token, body=None):
    request = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization': f"Bearer {token}", 'Content-Type': 'application/json'}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)['result']
    except urllib.error.HTTPError as error:
        try:
            errors = json.load(error).get('errors') or []
        except (json.JSONDecodeError, ValueError):
            errors = []
        message = '; '.join(item.get('message', '') for item in errors) or str(error)
        raise PublishError(f"Cloudflare said: {message}") from error


def _token_id(token, account):
    # a token made under My Profile is the user's, and one made under an account is the account's
    for path in ('/user/tokens/verify', account and f"/accounts/{account}/tokens/verify"):
        if not path:
            continue
        try:
            return _api('GET', path, token)['id']
        except PublishError:
            continue
    raise PublishError("Cloudflare doesn't accept CLOUDFLARE_API_TOKEN")


def _account(token):
    account = os.getenv('CLOUDFLARE_ACCOUNT_ID')
    if account:
        return account
    try:
        accounts = _api('GET', '/accounts', token)
    except PublishError:
        accounts = []
    if len(accounts) != 1:
        raise PublishError('Set CLOUDFLARE_ACCOUNT_ID, as the token can\'t say which account to use')
    return accounts[0]['id']


class Bucket:
    """
    The bucket exports go in, made and given its r2.dev address if it hasn't one
    """
    def __init__(self):
        import boto3
        from botocore.config import Config

        token = os.getenv('CLOUDFLARE_API_TOKEN')
        if not token:
            raise PublishError('Set CLOUDFLARE_API_TOKEN to publish exports')
        self.token = token
        self.account = _account(token)
        self.name = bucket_name()
        self._make()
        self.address = self._public_address()
        self.s3 = boto3.client(
            's3',
            endpoint_url=f"https://{self.account}.r2.cloudflarestorage.com",
            aws_access_key_id=_token_id(token, self.account),
            aws_secret_access_key=hashlib.sha256(token.encode()).hexdigest(),
            region_name='auto',
            # newer boto3 adds checksums to every request, which R2 hasn't always taken
            config=Config(
                retries={'max_attempts': 5, 'mode': 'standard'},
                request_checksum_calculation='when_required',
                response_checksum_validation='when_required'
            )
        )

    def _make(self):
        # through the API rather than S3, which says why when R2 isn't turned on for the account
        path = f"/accounts/{self.account}/r2/buckets"
        try:
            _api('GET', f"{path}/{self.name}", self.token)
        except PublishError:
            _api('POST', path, self.token, {'name': self.name})

    def _public_address(self):
        # r2.dev serves a bucket to anyone, without listing what's in it
        path = f"/accounts/{self.account}/r2/buckets/{self.name}/domains/managed"
        managed = _api('GET', path, self.token)
        if not managed.get('enabled'):
            managed = _api('PUT', path, self.token, {'enabled': True})
        address = f"https://{managed['domain']}"
        try:
            with open(ADDRESS_JSON, 'w') as fh:
                json.dump({'bucket': self.name, 'address': address}, fh)
        except OSError:
            pass
        return address

    def remote(self, prefix):
        # {key: size} of everything under a prefix
        found = {}
        for page in self.s3.get_paginator('list_objects_v2').paginate(Bucket=self.name, Prefix=prefix):
            for item in page.get('Contents', []):
                found[item['Key']] = item['Size']
        return found

    def _headers(self, key):
        content_type = mimetypes.guess_type(key)[0] or 'application/octet-stream'
        if content_type.startswith('text/') or content_type.endswith('javascript'):
            content_type += '; charset=utf-8'
        return content_type, PAGE_CACHE if key.endswith('.html') else FILE_CACHE

    def upload(self, path, key, on_bytes):
        from boto3.s3.transfer import TransferConfig

        content_type, cache = self._headers(key)
        self.s3.upload_file(
            path,
            self.name,
            key,
            ExtraArgs={'ContentType': content_type, 'CacheControl': cache},
            Config=TransferConfig(multipart_threshold=PART_BYTES, multipart_chunksize=PART_BYTES, max_concurrency=4),
            Callback=on_bytes
        )

    def copy(self, source, key):
        """
        A file already in the bucket under another name, copied there by R2 itself, which is
        quicker than sending it again: a big file is copied in parts, as it was sent
        """
        from boto3.s3.transfer import TransferConfig

        content_type, cache = self._headers(key)
        self.s3.copy(
            {'Bucket': self.name, 'Key': source},
            self.name,
            key,
            ExtraArgs={'ContentType': content_type, 'CacheControl': cache, 'MetadataDirective': 'REPLACE'},
            Config=TransferConfig(multipart_threshold=PART_BYTES, multipart_chunksize=PART_BYTES, max_concurrency=4)
        )

    def delete(self, keys):
        keys = list(keys)
        for start in range(0, len(keys), DELETE_BATCH):
            self.s3.delete_objects(
                Bucket=self.name,
                Delete={'Objects': [{'Key': key} for key in keys[start:start + DELETE_BATCH]], 'Quiet': True}
            )


def known_address():
    # the bucket's address from the last time anything was published, without asking Cloudflare
    try:
        with open(ADDRESS_JSON) as fh:
            saved = json.load(fh)
        return saved['address'] if saved.get('bucket') == bucket_name() else None
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None
