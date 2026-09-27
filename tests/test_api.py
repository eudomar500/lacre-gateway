"""Every endpoint through a test client, over FakeChain."""

import os
import secrets
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import support
from support import AGREE, API_KEY, KEYCACHE, VERIFIER, VERIFIER_OLD
from lacre_gateway.app import create_app
from lacre_gateway.chainio import load_account

KEY = ("amazon.com", "synthsel2026a")
AUTH = {"X-API-Key": API_KEY}


class AliveWorker(SimpleNamespace):
    def alive(self):
        return self.up


@pytest.fixture
def api(gw):
    gw.alive = AliveWorker(up=True)
    gw.client = TestClient(create_app(gw.settings, gw.store, gw.blobs, gw.contracts,
                                      gw.alive, clock=gw.clock))
    return gw


def upload(api, raw=None):
    return api.client.post("/attest", headers=AUTH,
                           files={"eml": ("m.eml", raw or support.amazon_eml(),
                                          "message/rfc822")})


# ---- the API key -----------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("post", "/attest"), ("get", "/jobs/x"), ("get", "/records/0"),
    ("get", "/senders/amazon.com/sel"), ("get", "/health")])
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong-" + "y" * 30}])
def test_every_endpoint_needs_the_key(api, method, path, headers):
    response = getattr(api.client, method)(path, headers=headers)
    assert response.status_code == 401


# ---- POST /attest ----------------------------------------------------------

def test_attest_returns_a_job_at_once(api):
    response = upload(api)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending" and body["job"] == "/jobs/%s" % (body["job_id"],)
    assert api.chain.sends() == []
    assert api.blobs.is_staged(body["job_id"])


def test_attest_stores_nothing_but_the_signed_headers(api):
    job_id = upload(api).json()["job_id"]
    staged = (api.blobs.staged / ("%s.txt" % (job_id,))).read_bytes()
    assert b"Received:" not in staged and b"Thank you" not in staged
    assert staged.startswith(b"DKIM-Signature:")
    assert oct(os.stat(api.blobs.staged / ("%s.txt" % (job_id,))).st_mode & 0o777) == "0o600"
    dump = "\n".join(api.store._db.iterdump()).lower()
    assert "customer@example.org" not in dump and "your amazon.com order" not in dump


def test_attest_refuses_mail_it_cannot_attest(api):
    raw = support.amazon_eml().replace(b"DKIM-Signature:", b"X-Old:")
    response = upload(api, raw)
    assert response.status_code == 422
    assert "no DKIM-Signature" in response.json()["detail"]
    assert api.store.open_jobs() == []


def test_attest_refuses_an_oversized_upload(api):
    api.client.app  # noqa: B018
    api.settings = support.settings(api.settings.data_dir, max_eml_bytes=100)
    client = TestClient(create_app(api.settings, api.store, api.blobs, api.contracts,
                                   api.alive, clock=api.clock))
    response = client.post("/attest", headers=AUTH,
                           files={"eml": ("m.eml", support.amazon_eml(), "message/rfc822")})
    assert response.status_code == 413


# ---- GET /jobs/{id} --------------------------------------------------------

def test_a_job_through_to_its_record(api):
    api.chain.keys[KEY] = support.active_key()
    job_id = upload(api).json()["job_id"]
    body = api.client.get("/jobs/%s" % (job_id,), headers=AUTH).json()
    assert body["status"] == "pending" and body["consensus_tx"] is None
    assert body["body_hash_matches"] is True

    api.worker.tick()
    body = api.client.get("/jobs/%s" % (job_id,), headers=AUTH).json()
    tx_id = api.chain.sends()[0][0]
    assert body["status"] == "attesting"
    assert body["consensus_tx"] == tx_id
    assert body["explorer"] == "%s/tx/%s" % (support.EXPLORER, tx_id)
    assert body["submitted_at"].endswith("Z")
    assert "record_id" not in body

    api.chain.script(tx_id, AGREE)
    chosen = SimpleNamespace(domain="amazon.com", selector="synthsel2026a",
                             bh=api.store.job(job_id)["bh"])
    support.our_record(api, chosen)
    api.worker.tick()
    body = api.client.get("/jobs/%s" % (job_id,), headers=AUTH).json()
    assert body["status"] == "finalized"
    assert body["record_id"] == "0" and body["verifier"] == VERIFIER
    assert body["finished_at"] and body["decided_at"]
    assert body["record"] == "/records/0?verifier=%s" % (VERIFIER,)
    assert body["valid_and_aligned"] is True


def test_a_finalized_job_with_an_invalid_record_says_so(api):
    api.chain.keys[KEY] = support.active_key()
    job_id = upload(api).json()["job_id"]
    api.worker.tick()
    api.chain.script(api.chain.sends()[0][0], AGREE)
    chosen = SimpleNamespace(domain="amazon.com", selector="synthsel2026a",
                             bh=api.store.job(job_id)["bh"])
    support.our_record(api, chosen, valid=False, reason="RSA PKCS#1 v1.5 check failed")
    api.worker.tick()
    body = api.client.get("/jobs/%s" % (job_id,), headers=AUTH).json()
    assert body["status"] == "finalized" and body["valid_and_aligned"] is False


def test_a_job_in_sender_verification_says_when(api):
    api.chain.keys[KEY] = dict(support.active_key(), state="pending",
                               first_seen="2026-09-21T12:00:00Z")
    job_id = upload(api).json()["job_id"]
    api.worker.tick()
    body = api.client.get("/jobs/%s" % (job_id,), headers=AUTH).json()
    assert body["stage"] == "sender in verification"
    assert body["sender_confirm_after"] == "2026-09-22T12:10:00Z"


def test_a_refused_job_names_the_reason(api):
    api.chain.keys[KEY] = dict(support.active_key(), state="retired")
    job_id = upload(api).json()["job_id"]
    api.worker.tick()
    body = api.client.get("/jobs/%s" % (job_id,), headers=AUTH).json()
    assert body["status"] == "refused" and body["refusal_reason"] == "key retired"


def test_an_unknown_job_is_404(api):
    assert api.client.get("/jobs/nope", headers=AUTH).status_code == 404


# ---- GET /records/{id} -----------------------------------------------------

def test_a_record_is_read_from_the_verifier_the_router_names_now(api):
    api.chain.add_record(domain="amazon.com")
    body = api.client.get("/records/0", headers=AUTH).json()
    assert body["verifier"] == VERIFIER and body["read_at"] == "LATEST_FINAL"
    assert body["valid_and_aligned"] is True
    assert body["record"]["domain"] == "amazon.com"
    reads = [(a, m, args, f) for a, m, args, f in api.chain.reads]
    assert (support.ROUTER, "resolve", ("verifier",), True) in reads
    assert (VERIFIER, "get", ("0",), True) in reads

    # The Router moves: the next read follows it, nothing was cached.
    api.chain.resolves["verifier"] = VERIFIER_OLD
    assert api.client.get("/records/0", headers=AUTH).status_code == 404


def test_an_earlier_verifier_is_read_only_if_the_router_named_it(api):
    api.chain.add_record(verifier=VERIFIER_OLD, valid=False)
    body = api.client.get("/records/0?verifier=%s" % (VERIFIER_OLD,), headers=AUTH).json()
    assert body["verifier"] == VERIFIER_OLD and body["current_verifier"] is False
    assert body["valid_and_aligned"] is False
    stranger = "0x" + "99" * 20
    response = api.client.get("/records/0?verifier=%s" % (stranger,), headers=AUTH)
    assert response.status_code == 400


def test_a_missing_record_is_404_and_a_bad_id_400(api):
    assert api.client.get("/records/5", headers=AUTH).status_code == 404
    assert api.client.get("/records/abc", headers=AUTH).status_code == 400


def test_a_chain_outage_is_503(api):
    api.chain.up = False
    assert api.client.get("/records/0", headers=AUTH).status_code == 503


# ---- GET /senders/{domain}/{selector} --------------------------------------

@pytest.mark.parametrize("state", ["active", "rotated", "retired"])
def test_sender_states(api, state):
    api.chain.keys[KEY] = dict(support.active_key(), state=state)
    body = api.client.get("/senders/Amazon.com./synthsel2026a", headers=AUTH).json()
    assert body["state"] == state and body["domain"] == "amazon.com"
    assert "n_hex" not in body and "e" not in body


def test_an_unknown_sender(api):
    body = api.client.get("/senders/example.org/none", headers=AUTH).json()
    assert body == {"domain": "example.org", "selector": "none", "state": "unknown"}


def test_a_pending_sender_says_when_it_can_be_confirmed(api):
    api.chain.keys[KEY] = dict(support.active_key(), state="pending",
                               first_seen="2026-09-21T12:00:00Z", activated_at="")
    api.clock.now = 1790000000.0
    body = api.client.get("/senders/amazon.com/synthsel2026a", headers=AUTH).json()
    assert body["state"] == "pending"
    assert body["confirm_after"] == "2026-09-22T12:00:00Z"
    assert body["can_confirm_now"] is False
    assert (KEYCACHE, "key_status", KEY, True) in api.chain.reads


# ---- GET /health -------------------------------------------------------------

def test_health_when_all_is_well(api):
    api.worker.tick()
    response = api.client.get("/health", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["chain"] and body["router"] and body["worker"]
    assert body["signer"] == "configured"


def test_health_reports_each_failure(api):
    api.worker.tick()
    api.chain.resolves["keycache"] = ""
    body = api.client.get("/health", headers=AUTH)
    assert body.status_code == 503 and body.json()["router"] is False
    api.alive.up = False
    assert api.client.get("/health", headers=AUTH).json()["worker"] is False
    api.chain.up = False
    assert api.client.get("/health", headers=AUTH).json()["chain"] is False


def test_health_says_the_worker_is_dead_when_it_stops_beating(api):
    api.worker.tick()
    api.clock.advance(api.settings.worker_stale_s + 1)
    assert api.client.get("/health", headers=AUTH).json()["worker"] is False


# ---- the served headers ------------------------------------------------------

def test_headers_are_served_only_while_the_call_is_in_flight(api):
    api.chain.keys[KEY] = support.active_key()
    job_id = upload(api).json()["job_id"]
    assert list(api.blobs.served.iterdir()) == []
    api.worker.tick()
    token = api.store.job(job_id)["blob_token"]
    served = api.client.get("/h/%s" % (token,))
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("text/plain")
    assert served.headers["cache-control"] == "no-store"
    assert served.content.startswith(b"DKIM-Signature:")
    assert api.client.get("/h/%s" % ("A" * 43,)).status_code == 404
    assert api.client.get("/h/..%2fgateway.sqlite3").status_code == 404

    api.chain.script(api.chain.sends()[0][0], AGREE)
    api.chain.refusal[VERIFIER] = "key pending"
    api.worker.tick()
    assert api.store.job(job_id)["status"] == "refused"
    assert api.client.get("/h/%s" % (token,)).status_code == 404


# ---- no key material -----------------------------------------------------------

def test_the_signing_key_file_must_be_owner_only_and_never_echoed(tmp_path):
    secret = "0x" + secrets.token_hex(32)
    path = tmp_path / "signing.key"
    path.write_text(secret + "\n")
    os.chmod(path, 0o644)
    with pytest.raises(PermissionError) as error:
        load_account(path)
    assert secret not in str(error.value)
    path.write_text("not a key " + secret[:20])
    os.chmod(path, 0o600)
    with pytest.raises(ValueError) as error:
        load_account(path)
    assert secret[:20] not in str(error.value)
    path.write_text(secret)
    account = load_account(path)
    assert account.address.startswith("0x")


def test_no_response_carries_key_material(api):
    api.chain.keys[KEY] = support.active_key()
    job_id = upload(api).json()["job_id"]
    api.worker.tick()
    texts = [api.client.get(p, headers=AUTH).text for p in (
        "/health", "/jobs/%s" % (job_id,), "/senders/amazon.com/synthsel2026a")]
    for text in texts:
        assert "c3c3c3" not in text and "private" not in text.lower()
