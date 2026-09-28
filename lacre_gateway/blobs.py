"""What validators fetch from the gateway: staged, then served, then deleted.

Two kinds of file go through this one code path. The signed headers of an
attest call are served at /h/{token}, a random 256 bit token; the body of
an extract call is served at /b/{name}.bin, 32 hex characters (128 bits)
from the same CSPRNG, the shape the tunnel config and the Extractor docs
expect.

A file is written to staged/ when the email is accepted, where nothing can
reach it, and moves to served/ under its random name only when the call
that needs it is about to be sent. From that moment the URL is public: it is
an argument of the call, and calldata is readable by anyone. That is why a
header blob holds nothing but the headers the signature covers (headers.py),
and why a body is kept only for jobs that asked for an extraction. The file
is deleted as soon as its call is FINALIZED, refused or failed, so it is
fetchable only while the validators may still need it.
"""

import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Kind:
    # Suffix of the files on disk, and of the public URL.
    suffix: str
    url_suffix: str
    name: re.Pattern
    new_name: object


HEADERS = Kind(suffix=".txt", url_suffix="", name=re.compile(r"^[A-Za-z0-9_-]{43}$"),
               new_name=lambda: secrets.token_urlsafe(32))
BODY = Kind(suffix=".bin", url_suffix=".bin", name=re.compile(r"^[0-9a-f]{32}$"),
            new_name=lambda: secrets.token_hex(16))


class BlobStore:
    def __init__(self, root, base_url, kind=HEADERS):
        self.root = Path(root)
        self.base_url = base_url.rstrip("/")
        self.kind = kind
        self.staged = self.root / "staged"
        self.served = self.root / "served"
        for path in (self.root, self.staged, self.served):
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)

    def _staged(self, job_id):
        return self.staged / ("%s%s" % (job_id, self.kind.suffix))

    def _served(self, token):
        return self.served / ("%s%s" % (token, self.kind.suffix))

    def _write(self, path, data):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)

    def stage(self, job_id, blob):
        self._write(self._staged(job_id), blob)

    def publish(self, job_id):
        """Move a staged blob to a fresh unguessable path: (token, url)."""
        token = self.kind.new_name()
        os.replace(self._staged(job_id), self._served(token))
        return token, self.url(token)

    def url(self, token):
        return "%s/%s%s" % (self.base_url, token, self.kind.url_suffix)

    def read(self, token):
        """The served blob for token, or None."""
        if not self.kind.name.match(token or ""):
            return None
        try:
            return self._served(token).read_bytes()
        except OSError:
            return None

    def is_staged(self, job_id):
        return self._staged(job_id).exists()

    def delete(self, job_id, token=None):
        paths = [self._staged(job_id)]
        if token and self.kind.name.match(token):
            paths.append(self._served(token))
        for path in paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
