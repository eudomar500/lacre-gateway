#!/usr/bin/env python3
"""Run the gateway locally against the recorded fixtures. No network, no key.

The chain is tests/support.FakeChain: every sender key reads as active, and
every call the worker sends goes PENDING, ACCEPTED, then FINALIZED with the
recorded AGREE state of consensus_call37_agree.json, writing a record that
matches it. Everything else, the API, the store, the blob files and the
worker, is the real code.

    .venv/bin/python scripts/run_offline.py
    curl -s -H "X-API-Key: $KEY" -F eml=@tests/fixtures/amazon_layout.eml \
        http://127.0.0.1:8080/attest

The API key is printed at start. Data goes to a temporary directory that is
removed on exit.
"""

import logging
import secrets
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import uvicorn  # noqa: E402

import support  # noqa: E402
from lacre_gateway.app import create_app  # noqa: E402
from lacre_gateway.blobs import BlobStore  # noqa: E402
from lacre_gateway.contracts import Contracts  # noqa: E402
from lacre_gateway.store import Store  # noqa: E402
from lacre_gateway.worker import Worker  # noqa: E402


class OfflineChain(support.FakeChain):
    def __init__(self, store):
        super().__init__()
        self.store = store

    def view(self, address, method, args, final=True):
        if address == support.KEYCACHE and method == "key_status":
            return support.active_key(args[0], args[1])
        return super().view(address, method, args, final)

    def send(self, address, method, args, value=0):
        tx_id = super().send(address, method, args, value)
        self.script(tx_id, support.PENDING, support.ACCEPTED, support.AGREE)
        if method == "attest":
            self.on_final[tx_id] = lambda: self.add_record(
                domain=args[1], selector=args[2], bh=self.bh_of(tx_id), fee_paid=str(value))
        return tx_id

    def bh_of(self, tx_id):
        for job in self.store.open_jobs():
            if job["current_tx"] == tx_id:
                return job["bh"]
        return ""


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    data = tempfile.TemporaryDirectory(prefix="lacre-offline-")
    key = secrets.token_urlsafe(32)
    settings = support.settings(data.name, api_keys=(key,), poll_s=2,
                                blob_base_url="https://localhost.invalid/h")
    store = Store(settings.db_path)
    blobs = BlobStore(settings.blob_dir, settings.blob_base_url)
    contracts = Contracts(OfflineChain(store), settings.router)
    worker = Worker(settings, store, blobs, contracts)
    app = create_app(settings, store, blobs, contracts, worker)
    worker.start()
    print("API key: %s" % (key,), flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=8080, log_level="info", access_log=False)
    finally:
        worker.stop()
        data.cleanup()


if __name__ == "__main__":
    main()
