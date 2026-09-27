"""The signed headers on disk: staged, then served, then deleted.

A blob is written to staged/ when the email is accepted, where nothing can
reach it, and moves to served/ under a random 256 bit token only when the
attest call is about to be sent. From that moment the URL is public: it is
an argument of the attest call, and calldata is readable by anyone. That is
why a blob holds nothing but the headers the signature covers (headers.py).
The file is deleted as soon as the job is finalized, refused or failed, so
it is fetchable only while the validators may still need it.
"""

import os
import re
import secrets
from pathlib import Path

TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")


class BlobStore:
    def __init__(self, root, base_url):
        self.root = Path(root)
        self.base_url = base_url.rstrip("/")
        self.staged = self.root / "staged"
        self.served = self.root / "served"
        for path in (self.root, self.staged, self.served):
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)

    def _write(self, path, data):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)

    def stage(self, job_id, blob):
        self._write(self.staged / ("%s.txt" % (job_id,)), blob)

    def publish(self, job_id):
        """Move a staged blob to a fresh unguessable path: (token, url)."""
        token = secrets.token_urlsafe(32)
        os.replace(self.staged / ("%s.txt" % (job_id,)), self.served / ("%s.txt" % (token,)))
        return token, self.url(token)

    def url(self, token):
        return "%s/%s" % (self.base_url, token)

    def read(self, token):
        """The served blob for token, or None."""
        if not TOKEN.match(token or ""):
            return None
        try:
            return (self.served / ("%s.txt" % (token,))).read_bytes()
        except OSError:
            return None

    def is_staged(self, job_id):
        return (self.staged / ("%s.txt" % (job_id,))).exists()

    def delete(self, job_id, token=None):
        paths = [self.staged / ("%s.txt" % (job_id,))]
        if token and TOKEN.match(token):
            paths.append(self.served / ("%s.txt" % (token,)))
        for path in paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
