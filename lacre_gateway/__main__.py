"""python -m lacre_gateway: the API and the worker in one process.

Listens on LACRE_LISTEN_HOST:LACRE_LISTEN_PORT (127.0.0.1:8080 by default);
cloudflared is the only thing that should reach it.
"""

import logging
import os
import socket
import sys

import uvicorn

from . import config
from .app import create_app
from .blobs import BODY, BlobStore
from .chainio import Chain
from .contracts import Contracts
from .store import Store
from .vendor import txstate
from .worker import Worker


def silence_vendor_output():
    """Send the public tools' print() output nowhere.

    tools/chain.py and tools/attest.py narrate every step on stdout for a
    person at a terminal, including raw gen_call responses when a read
    fails. None of that belongs in the service log. The gateway logs through
    the logging module, to stderr, and says only what it chose to say.
    """
    sys.stdout = open(os.devnull, "w")


def main():
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(levelname)s %(name)s %(message)s")
    try:
        settings = config.load()
    except config.ConfigError as error:
        print("config: %s" % (error,), file=sys.stderr)
        return 1
    silence_vendor_output()
    # The SDK calls requests without a timeout; a stalled connection has to
    # fail so the next pass can resume (as tools/attest.py main does).
    socket.setdefaulttimeout(txstate.SOCKET_TIMEOUT)

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(settings.data_dir, 0o700)
    store = Store(settings.db_path)
    blobs = BlobStore(settings.blob_dir, settings.blob_base_url)
    bodies = BlobStore(settings.body_dir, settings.body_url, BODY)
    contracts = Contracts(Chain(settings.network, settings.signing_key_file), settings.router)
    worker = Worker(settings, store, blobs, contracts, bodies=bodies)
    app = create_app(settings, store, blobs, contracts, worker, bodies=bodies)
    worker.start()
    try:
        uvicorn.run(app, host=os.environ.get("LACRE_LISTEN_HOST", "127.0.0.1"),
                    port=int(os.environ.get("LACRE_LISTEN_PORT", "8080")),
                    log_level="info", access_log=False)
    finally:
        worker.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
