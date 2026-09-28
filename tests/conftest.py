"""Shared wiring for the tests. Nothing here opens a socket."""

import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import support  # noqa: E402
from lacre_gateway.blobs import BODY, BlobStore  # noqa: E402
from lacre_gateway.contracts import Contracts  # noqa: E402
from lacre_gateway.store import Store  # noqa: E402
from lacre_gateway.worker import Worker  # noqa: E402


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any attempt to open a connection fails the test."""
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to use the network")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def gw(tmp_path):
    """A gateway over FakeChain with a fake clock, the worker not started."""
    clock = support.Clock()
    settings = support.settings(tmp_path)
    store = Store(settings.db_path, clock=clock)
    blobs = BlobStore(settings.blob_dir, settings.blob_base_url)
    bodies = BlobStore(settings.body_dir, settings.body_url, BODY)
    chain = support.FakeChain()
    contracts = Contracts(chain, settings.router)
    worker = Worker(settings, store, blobs, contracts, clock=clock, bodies=bodies)
    return SimpleNamespace(settings=settings, store=store, blobs=blobs, bodies=bodies,
                           chain=chain, contracts=contracts, worker=worker, clock=clock)
