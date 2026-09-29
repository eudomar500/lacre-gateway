"""The MCP tools: over the real app in process, over a scripted gateway
(httpx.MockTransport) for the answers FakeChain cannot give, and the /mcp
mount through a test client."""

import base64
import json
import logging

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client

import support
from support import API_KEY, EXTRACTOR, VERIFIER
from lacre_gateway.app import create_app
from lacre_mcp.gateway import Gateway
from lacre_mcp.server import build_server, message_bytes

KEY = ("amazon.com", "synthsel2026a")
MCP_HEADERS = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}


def call(server, name, arguments=None):
    """(is_error, the decoded answer or the error text) of one tool call."""
    async def go():
        async with Client(server) as client:
            return await client.call_tool(name, arguments or {})
    result = anyio.run(go)
    text = result.content[0].text
    return (True, text) if result.is_error else (False, json.loads(text))


@pytest.fixture
def api(gw):
    gw.app = create_app(gw.settings, gw.store, gw.blobs, gw.contracts, clock=gw.clock,
                        bodies=gw.bodies)
    transport = httpx.ASGITransport(app=gw.app, raise_app_exceptions=False)
    gw.mcp = build_server(lambda ctx: Gateway("http://gateway", API_KEY, transport=transport))
    return gw


class Scripted:
    """A gateway that answers from a list, one answer per request; the
    last repeats. An answer is (status, json body) or an exception."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return httpx.Response(status, json=body)


def scripted(*answers, **options):
    gateway = Scripted(*answers)
    transport = httpx.MockTransport(gateway)
    server = build_server(lambda ctx: Gateway("http://gateway", API_KEY, transport=transport),
                          **options)
    return gateway, server


class FakeTime:
    """sleep that only moves a clock, and records what it was asked for."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def job(status, **fields):
    return dict({"id": "ab" * 16, "status": status, "stage": status}, **fields)


# ---- the tool list ----------------------------------------------------------

def test_the_tools_are_the_documented_set(api):
    names = [t.name for t in anyio.run(api.mcp.list_tools)]
    assert names == ["lacre_attest", "lacre_job", "lacre_wait_job", "lacre_record",
                     "lacre_extraction", "lacre_sender", "lacre_health",
                     "lacre_mailbox_create", "lacre_mailboxes", "lacre_mailbox",
                     "lacre_mailbox_jobs", "lacre_mailbox_disable"]


def test_descriptions_are_plain_ascii(api):
    for tool in anyio.run(api.mcp.list_tools):
        assert tool.description and tool.description.isascii(), tool.name


# ---- lacre_attest -----------------------------------------------------------

def test_attest_takes_the_message_as_text(api):
    error, stub = call(api.mcp, "lacre_attest",
                       {"eml": support.amazon_eml().decode("ascii"), "extract": "none"})
    assert not error
    assert stub["status"] == "pending" and stub["extract"] == "none"
    assert api.store.job(stub["job_id"])["headers_sha256"] == \
        support.submit(api)[1].headers_sha256


def test_attest_asks_for_auto_by_default(api):
    _, stub = call(api.mcp, "lacre_attest", {"eml": support.amazon_eml().decode("ascii")})
    assert stub["extract"] == "auto"
    assert api.bodies.is_staged(stub["job_id"])


def test_attest_takes_the_message_as_base64(api):
    encoded = base64.b64encode(support.amazon_eml()).decode("ascii")
    # Wrapped at 76 columns, as base64 tools print it.
    wrapped = "\n".join(encoded[i:i + 76] for i in range(0, len(encoded), 76))
    error, stub = call(api.mcp, "lacre_attest", {"eml": wrapped, "extract": "none"})
    assert not error
    assert api.store.job(stub["job_id"])["headers_sha256"] == \
        support.submit(api)[1].headers_sha256


def test_attest_restores_crlf_in_a_message_saved_with_lf():
    raw = support.amazon_eml()
    assert message_bytes(raw.replace(b"\r\n", b"\n").decode("ascii")) == raw
    # A message that has CRs is sent as it is.
    assert message_bytes(raw.decode("ascii")) == raw


def test_attest_refuses_what_is_neither_a_message_nor_base64(api):
    for eml in ("hello there", "Zm9vYmFy", "From someone Mon Sep 28 2026\nSubject: x\n\nbody"):
        error, text = call(api.mcp, "lacre_attest", {"eml": eml})
        assert error and "neither an RFC 5322 message" in text
    assert api.store.open_jobs() == []


def test_attest_passes_the_gateways_refusal_on(api):
    raw = support.amazon_eml().replace(b"DKIM-Signature:", b"X-Old:")
    error, text = call(api.mcp, "lacre_attest", {"eml": raw.decode("ascii")})
    assert error
    assert "422" in text and "no DKIM-Signature" in text


def test_attest_rejects_an_unknown_mode_before_the_gateway(api):
    error, text = call(api.mcp, "lacre_attest",
                       {"eml": support.amazon_eml().decode("ascii"), "extract": "all"})
    assert error and "extract" in text
    assert api.store.open_jobs() == []


# ---- jobs -------------------------------------------------------------------

def test_job_reads_a_job(api):
    _, stub = call(api.mcp, "lacre_attest",
                   {"eml": support.amazon_eml().decode("ascii"), "extract": "none"})
    error, body = call(api.mcp, "lacre_job", {"job_id": stub["job_id"]})
    assert not error
    assert body["id"] == stub["job_id"] and body["status"] == "pending"
    assert body["sender"] == {"domain": KEY[0], "selector": KEY[1]}


def test_an_unknown_job_is_a_tool_error_with_the_gateways_detail(api):
    error, text = call(api.mcp, "lacre_job", {"job_id": "0" * 32})
    assert error
    assert "404" in text and "no such job" in text


def test_an_argument_cannot_reach_another_endpoint(api):
    error, text = call(api.mcp, "lacre_job", {"job_id": "../health"})
    assert error and "404" in text and "chain" not in text
    gateway, server = scripted((404, {"detail": "no such job"}))
    call(server, "lacre_job", {"job_id": "../health?x=1"})
    assert gateway.requests[0].url.raw_path == b"/jobs/..%2Fhealth%3Fx%3D1"


def test_a_5xx_is_a_tool_error_with_the_gateways_detail():
    _, server = scripted((503, {"detail": "chain unavailable"}))
    error, text = call(server, "lacre_job", {"job_id": "ab" * 16})
    assert error
    assert "503" in text and "chain unavailable" in text


def test_a_5xx_from_the_real_app_is_a_tool_error(api):
    api.chain.up = False
    error, text = call(api.mcp, "lacre_record", {"record_id": "0"})
    assert error and "503" in text and "chain unavailable" in text


def test_an_unreachable_gateway_is_a_tool_error_and_the_server_goes_on():
    gateway, server = scripted(httpx.ConnectError("connection refused"),
                               (200, job("pending")))
    error, text = call(server, "lacre_job", {"job_id": "ab" * 16})
    assert error and "could not be reached" in text and "ConnectError" in text
    error, body = call(server, "lacre_job", {"job_id": "ab" * 16})
    assert not error and body["status"] == "pending"


def test_a_body_that_is_not_json_is_quoted_short():
    def answer(request):
        return httpx.Response(502, text="<html>" + "x" * 5000 + "</html>")
    transport = httpx.MockTransport(answer)
    server = build_server(lambda ctx: Gateway("http://gateway", API_KEY, transport=transport))
    error, text = call(server, "lacre_job", {"job_id": "ab" * 16})
    assert error and "502" in text and len(text) < 700


def test_without_a_key_every_tool_says_so():
    server = build_server(lambda ctx: Gateway("http://gateway", ""))
    error, text = call(server, "lacre_health")
    assert error and "LACRE_API_KEY" in text


def test_the_key_goes_in_the_header_and_nowhere_else():
    gateway, server = scripted((200, job("pending")))
    call(server, "lacre_job", {"job_id": "ab" * 16})
    request = gateway.requests[0]
    assert request.headers["X-API-Key"] == API_KEY
    assert API_KEY not in str(request.url)


# ---- lacre_wait_job ---------------------------------------------------------

def test_wait_polls_every_30_seconds_until_finalized():
    time = FakeTime()
    gateway, server = scripted((200, job("pending")), (200, job("attesting")),
                               (200, job("finalized", record_id="7")),
                               sleep=time.sleep, clock=time.clock)
    error, body = call(server, "lacre_wait_job", {"job_id": "ab" * 16})
    assert not error
    assert body["status"] == "finalized" and body["record_id"] == "7"
    assert time.slept == [30, 30]
    assert len(gateway.requests) == 3


@pytest.mark.parametrize("status", ["refused", "failed"])
def test_wait_stops_at_every_final_status(status):
    time = FakeTime()
    _, server = scripted((200, job(status)), sleep=time.sleep, clock=time.clock)
    error, body = call(server, "lacre_wait_job", {"job_id": "ab" * 16})
    assert not error and body["status"] == status and time.slept == []


def test_wait_returns_the_last_read_at_the_timeout():
    time = FakeTime()
    gateway, server = scripted((200, job("attesting", stage="sender in verification")),
                               sleep=time.sleep, clock=time.clock)
    error, body = call(server, "lacre_wait_job", {"job_id": "ab" * 16, "timeout_s": 75})
    assert not error
    assert body["status"] == "attesting"
    # 30 + 30 + the 15 left: never past the timeout.
    assert time.slept == [30, 30, 15]
    assert len(gateway.requests) == 4


def test_wait_defaults_to_an_hour():
    time = FakeTime()
    gateway, server = scripted((200, job("pending")), sleep=time.sleep, clock=time.clock)
    call(server, "lacre_wait_job", {"job_id": "ab" * 16})
    assert sum(time.slept) == 3600 and len(gateway.requests) == 121


def test_wait_rides_out_5xx_and_network_errors():
    time = FakeTime()
    _, server = scripted((503, {"detail": "chain unavailable"}),
                         httpx.ReadTimeout("timed out"), (200, job("finalized")),
                         sleep=time.sleep, clock=time.clock)
    error, body = call(server, "lacre_wait_job", {"job_id": "ab" * 16})
    assert not error and body["status"] == "finalized"
    assert time.slept == [30, 30]


def test_wait_ends_on_a_4xx():
    time = FakeTime()
    _, server = scripted((404, {"detail": "no such job"}), sleep=time.sleep, clock=time.clock)
    error, text = call(server, "lacre_wait_job", {"job_id": "ab" * 16})
    assert error and "404" in text and "no such job" in text
    assert time.slept == []


def test_wait_that_never_read_the_job_is_an_error():
    time = FakeTime()
    _, server = scripted((503, {"detail": "chain unavailable"}),
                         sleep=time.sleep, clock=time.clock)
    error, text = call(server, "lacre_wait_job", {"job_id": "ab" * 16, "timeout_s": 60})
    assert error and "chain unavailable" in text


# ---- records, extractions, senders, health ----------------------------------

def test_record_reads_the_verifier_record(api):
    record_id = api.chain.add_record(domain=KEY[0], selector=KEY[1])
    error, body = call(api.mcp, "lacre_record", {"record_id": record_id, "verifier": VERIFIER})
    assert not error
    assert body["verifier"] == VERIFIER and body["record_id"] == record_id
    assert body["read_at"] == "LATEST_FINAL" and "valid_and_aligned" in body


def test_record_refuses_a_verifier_the_router_never_named(api):
    record_id = api.chain.add_record()
    error, text = call(api.mcp, "lacre_record",
                       {"record_id": record_id, "verifier": "0x" + "99" * 20})
    assert error and "400" in text and "not a Verifier the Router has named" in text


def test_extraction_reads_one_lane(api):
    ext_id = api.chain.add_extraction(EXTRACTOR)
    error, body = call(api.mcp, "lacre_extraction", {"lane": "patterns", "record_id": ext_id})
    assert not error
    assert body["lane"] == "patterns" and body["extractor"] == EXTRACTOR
    assert body["record"]["match"] is True


def test_extraction_lane_is_checked_before_the_gateway(api):
    error, text = call(api.mcp, "lacre_extraction", {"lane": "regex", "record_id": "0"})
    assert error and "lane" in text


def test_sender_reads_the_key_state(api):
    api.chain.keys[KEY] = support.active_key()
    error, body = call(api.mcp, "lacre_sender", {"domain": KEY[0], "selector": KEY[1]})
    assert not error
    assert body["state"] == "active" and body["domain"] == KEY[0]


def test_health_reports_a_503_as_its_answer(api):
    # No worker is running here, so the gateway answers 503 with its checks.
    error, body = call(api.mcp, "lacre_health")
    assert not error
    assert body["ok"] is False and body["worker"] is False and body["chain"] is True


def test_health_ok():
    _, server = scripted((200, {"chain": True, "router": True, "worker": True,
                                "signer": "configured"}))
    error, body = call(server, "lacre_health")
    assert not error and body["ok"] is True


def test_health_that_is_not_the_checks_is_an_error():
    _, server = scripted((401, {"detail": "missing or unknown API key"}))
    error, text = call(server, "lacre_health")
    assert error and "401" in text


# ---- mailboxes --------------------------------------------------------------

def test_mailbox_lifecycle(api):
    error, box = call(api.mcp, "lacre_mailbox_create", {"extract": "none"})
    assert not error
    assert box["address"] == "lacre-%s@in-sidr.xyz" % (box["id"],)
    assert box["extract"] == "none" and box["enabled"] is True

    _, listed = call(api.mcp, "lacre_mailboxes")
    assert [b["id"] for b in listed["mailboxes"]] == [box["id"]]

    _, one = call(api.mcp, "lacre_mailbox", {"mailbox_id": box["id"]})
    assert one["id"] == box["id"]

    _, jobs = call(api.mcp, "lacre_mailbox_jobs",
                   {"mailbox_id": box["id"], "limit": 5, "offset": 0})
    assert jobs == {"jobs": [], "limit": 5, "offset": 0, "next": None}

    _, disabled = call(api.mcp, "lacre_mailbox_disable", {"mailbox_id": box["id"]})
    assert disabled["enabled"] is False and disabled["disabled_at"]


def test_mailbox_create_defaults_to_auto(api):
    _, box = call(api.mcp, "lacre_mailbox_create")
    assert box["extract"] == "auto"


def test_an_unknown_mailbox_is_a_tool_error(api):
    for name in ("lacre_mailbox", "lacre_mailbox_jobs", "lacre_mailbox_disable"):
        error, text = call(api.mcp, name, {"mailbox_id": "a" * 12})
        assert error and "404" in text and "no such mailbox" in text


def test_mailbox_jobs_limit_is_checked(api):
    error, text = call(api.mcp, "lacre_mailbox_jobs", {"mailbox_id": "a" * 12, "limit": 500})
    assert error and "limit" in text


# ---- what is logged ---------------------------------------------------------

def test_neither_the_message_nor_the_key_is_logged(api, caplog):
    caplog.set_level(logging.DEBUG)
    raw = support.amazon_eml()
    call(api.mcp, "lacre_attest", {"eml": raw.decode("ascii"), "extract": "none"})
    call(api.mcp, "lacre_attest", {"eml": raw.replace(b"DKIM-Signature:", b"X-Old:")
                                   .decode("ascii")})
    call(api.mcp, "lacre_attest", {"eml": "not a message"})
    assert API_KEY not in caplog.text
    for piece in (b"Thank you", b"customer@example.org", b"Subject:"):
        assert piece.decode() not in caplog.text


# ---- the /mcp mount ---------------------------------------------------------

def rpc(method, params=None, id=1):
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}


def sse_result(response):
    data = [line[len("data:"):] for line in response.text.splitlines()
            if line.startswith("data:")]
    return json.loads(data[-1])


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong-" + "y" * 30}])
def test_mcp_needs_the_key(api, path, headers):
    with TestClient(api.app) as client:
        response = client.post(path, json=rpc("tools/list"), headers=dict(MCP_HEADERS, **headers))
    assert response.status_code == 401
    assert response.json() == {"detail": "missing or unknown API key"}


def test_mcp_tools_use_the_key_of_the_request(api):
    other = "other-key-" + "z" * 24
    api.settings = support.settings(api.settings.data_dir, api_keys=(API_KEY, other))
    api.app = create_app(api.settings, api.store, api.blobs, api.contracts, clock=api.clock,
                         bodies=api.bodies)
    mine = dict(MCP_HEADERS, **{"X-API-Key": API_KEY})
    theirs = dict(MCP_HEADERS, **{"X-API-Key": other})
    with TestClient(api.app) as client:
        listed = sse_result(client.post("/mcp", json=rpc("tools/list"), headers=mine))
        assert len(listed["result"]["tools"]) == 12

        made = sse_result(client.post("/mcp", headers=mine, json=rpc(
            "tools/call", {"name": "lacre_mailbox_create", "arguments": {"extract": "none"}})))
        box = json.loads(made["result"]["content"][0]["text"])

        # The mailbox belongs to the key that created it over /mcp.
        seen = sse_result(client.post("/mcp", headers=theirs, json=rpc(
            "tools/call", {"name": "lacre_mailboxes", "arguments": {}})))
        assert json.loads(seen["result"]["content"][0]["text"]) == {"mailboxes": []}
        response = client.get("/mailboxes/%s" % (box["id"],), headers={"X-API-Key": API_KEY})
        assert response.status_code == 200
