"""POST /rpc: the Bradbury RPC with every request id renumbered.

The upstream is an httpx.MockTransport that answers as the Bradbury RPC
does: an id that is not an integer is a -32700, anything else is echoed
back with its id. Nothing here opens a socket.
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import support
from lacre_gateway.app import create_app
from lacre_gateway.rpcproxy import RPC_BATCH_MAX, RPC_MAX_BYTES, RPC_PER_IP, RPC_WINDOW_S
from lacre_gateway.web import BRADBURY_RPC

JSON = {"Content-Type": "application/json"}


class Upstream:
    """The Bradbury RPC: refuses a non-integer id, as the real one does."""

    def __init__(self):
        self.seen = []
        self.reply = None

    def answer_one(self, call):
        if "id" not in call:
            return None
        if call["id"] is not None and not isinstance(call["id"], int):
            return {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "Request.id of type int"}}
        if call["method"] == "eth_call":
            return {"jsonrpc": "2.0", "id": call["id"],
                    "error": {"code": 3, "message": "execution reverted", "data": "0x08c379a0"}}
        return {"jsonrpc": "2.0", "id": call["id"], "result": "0x107d"}

    def __call__(self, request):
        assert str(request.url) == BRADBURY_RPC
        assert "cf-connecting-ip" not in request.headers
        body = json.loads(request.content)
        self.seen.append(body)
        if self.reply is not None:
            return self.reply(body)
        if isinstance(body, list):
            out = [a for a in (self.answer_one(c) for c in body) if a is not None]
            return httpx.Response(200, json=out) if out else httpx.Response(200, content=b"")
        answer = self.answer_one(body)
        return httpx.Response(200, json=answer) if answer else httpx.Response(200, content=b"")


@pytest.fixture
def rpc(gw):
    gw.upstream = Upstream()
    gw.settings = support.settings(gw.settings.data_dir)
    gw.client = TestClient(create_app(gw.settings, gw.store, gw.blobs, gw.contracts,
                                      clock=gw.clock, rpc_transport=httpx.MockTransport(gw.upstream)))
    return gw


def call(id_, method="eth_chainId", **extra):
    body = {"jsonrpc": "2.0", "method": method, "params": []}
    if id_ is not ...:
        body["id"] = id_
    body.update(extra)
    return body


def post(rpc, body, ip="203.0.113.9", **kwargs):
    headers = dict(JSON, **{"CF-Connecting-IP": ip})
    if isinstance(body, bytes):
        return rpc.client.post("/rpc", content=body, headers=headers, **kwargs)
    return rpc.client.post("/rpc", content=json.dumps(body), headers=headers, **kwargs)


def test_a_string_id_is_forwarded_as_an_integer_and_restored(rpc):
    response = post(rpc, call("3f9e8c1a-metamask"))
    assert response.status_code == 200
    assert response.json() == {"jsonrpc": "2.0", "id": "3f9e8c1a-metamask", "result": "0x107d"}
    assert rpc.upstream.seen == [call(1)]
    assert response.headers["access-control-allow-origin"] == "*"


def test_a_numeric_id_is_renumbered_and_restored(rpc):
    response = post(rpc, call(4242424242))
    assert response.json() == {"jsonrpc": "2.0", "id": 4242424242, "result": "0x107d"}
    assert rpc.upstream.seen[0]["id"] == 1


def test_a_batch_with_mixed_ids_keeps_each_id_its_own(rpc):
    batch = [call("a"), call(7), call(None), call(...), call("7"), call(1)]
    response = post(rpc, batch)
    assert response.status_code == 200
    # Integers from 1 in order; the null id and the notification as they came.
    assert [c.get("id", "absent") for c in rpc.upstream.seen[0]] == [1, 2, None, "absent", 3, 4]
    assert [a["id"] for a in response.json()] == ["a", 7, None, "7", 1]
    assert all(a["result"] == "0x107d" for a in response.json())


def test_upstream_errors_pass_through_with_the_id_restored(rpc):
    response = post(rpc, call("x1", method="eth_call"))
    assert response.status_code == 200
    assert response.json() == {"jsonrpc": "2.0", "id": "x1", "error": {
        "code": 3, "message": "execution reverted", "data": "0x08c379a0"}}


def test_an_upstream_http_error_keeps_its_status_and_body(rpc):
    rpc.upstream.reply = lambda body: httpx.Response(503, json={
        "jsonrpc": "2.0", "id": body["id"], "error": {"code": -32603, "message": "busy"}})
    response = post(rpc, call("s"))
    assert response.status_code == 503
    assert response.json()["id"] == "s"
    assert response.json()["error"]["message"] == "busy"


def test_an_upstream_answer_that_is_not_json_is_a_502(rpc):
    rpc.upstream.reply = lambda body: httpx.Response(520, content=b"<!DOCTYPE html>520")
    response = post(rpc, call("s"))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == -32603


def test_an_upstream_timeout_is_a_504(rpc):
    def slow(body):
        raise httpx.ReadTimeout("slow")
    rpc.upstream.reply = slow
    assert post(rpc, call("s")).status_code == 504


def test_notifications_alone_get_no_body(rpc):
    response = post(rpc, call(...))
    assert response.status_code == 204
    assert response.content == b""


def test_an_oversize_body_is_refused_unforwarded(rpc):
    big = call("big", method="eth_sendRawTransaction", params=["0x" + "ab" * RPC_MAX_BYTES])
    response = post(rpc, big)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == -32600
    assert rpc.upstream.seen == []


def test_a_body_just_under_the_cap_is_forwarded(rpc):
    body = call("ok", method="eth_sendRawTransaction", params=["0x"])
    pad = RPC_MAX_BYTES - len(json.dumps(body)) - 2
    body["params"] = ["0x" + "a" * pad]
    assert len(json.dumps(body)) <= RPC_MAX_BYTES
    assert post(rpc, body).status_code == 200


def test_the_rate_limit_is_per_address(rpc):
    for _ in range(RPC_PER_IP):
        assert post(rpc, call(1)).status_code == 200
    refused = post(rpc, call(1))
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == -32005
    assert refused.headers["retry-after"] == str(RPC_WINDOW_S)
    assert len(rpc.upstream.seen) == RPC_PER_IP
    # Another address is not held back by this one, and the window passes.
    assert post(rpc, call(1), ip="198.51.100.4").status_code == 200
    rpc.clock.advance(RPC_WINDOW_S + 1)
    assert post(rpc, call(1)).status_code == 200


@pytest.mark.parametrize("body", [
    {"jsonrpc": "1.0", "id": 1, "method": "eth_chainId"},
    {"id": 1, "method": "eth_chainId"},
    {"jsonrpc": "2.0", "id": 1},
    {"jsonrpc": "2.0", "id": 1, "method": 5},
    {"jsonrpc": "2.0", "id": [1], "method": "eth_chainId"},
    {"jsonrpc": "2.0", "id": True, "method": "eth_chainId"},
    [],
    [call(1), "nope"],
    [call(i) for i in range(RPC_BATCH_MAX + 1)],
    "eth_chainId",
    42,
])
def test_anything_not_json_rpc_2_is_refused(rpc, body):
    response = post(rpc, body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600
    assert rpc.upstream.seen == []


def test_a_body_that_is_not_json_is_a_parse_error(rpc):
    response = post(rpc, b"{not json")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32700


def test_only_json_is_taken(rpc):
    response = rpc.client.post("/rpc", content=json.dumps(call(1)),
                               headers={"Content-Type": "text/plain"})
    assert response.status_code == 415
    assert rpc.upstream.seen == []


def test_json_with_a_charset_is_taken(rpc):
    response = rpc.client.post("/rpc", content=json.dumps(call("c")),
                               headers={"Content-Type": "application/json; charset=utf-8"})
    assert response.json()["id"] == "c"


def test_the_preflight_is_open_to_every_origin(rpc):
    response = rpc.client.options("/rpc", headers={
        "Origin": "chrome-extension://abc", "Access-Control-Request-Method": "POST"})
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "*"
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "Content-Type" in response.headers["access-control-allow-headers"]
