"""Mailboxes and POST /inbound, through a test client over FakeChain.

A delivery is signed the way deploy/worker/src/index.js signs it: hex
HMAC-SHA256 with the inbound secret over the timestamp, a newline, the
recipient, a newline and the raw message.
"""

import hashlib
import hmac
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

import support
from support import AGREE, API_KEY, EXTRACTOR, INBOUND_SECRET, VERIFIER
from lacre_gateway.app import create_app, inbound_mac
from lacre_gateway.store import Store

KEY = ("amazon.com", "synthsel2026a")
OTHER_KEY = "other-key-" + "z" * 24
AUTH = {"X-API-Key": API_KEY}
OTHER = {"X-API-Key": OTHER_KEY}


@pytest.fixture
def api(gw):
    gw.settings = support.settings(gw.settings.data_dir, api_keys=(API_KEY, OTHER_KEY))
    gw.client = TestClient(create_app(gw.settings, gw.store, gw.blobs, gw.contracts,
                                      clock=gw.clock, bodies=gw.bodies))
    return gw


def new_mailbox(api, auth=AUTH, extract=None):
    data = {"extract": extract} if extract else None
    response = api.client.post("/mailboxes", headers=auth, data=data)
    assert response.status_code == 201, response.text
    return response.json()


def sign(raw, recipient, stamp, secret=INBOUND_SECRET):
    mac = hmac.new(secret.encode(), b"%s\n%s\n" % (str(stamp).encode(), recipient.encode()),
                   hashlib.sha256)
    mac.update(raw)
    return mac.hexdigest()


def deliver(api, recipient, raw=None, stamp=None, signature=None, client=None):
    raw = raw if raw is not None else support.amazon_eml()
    stamp = int(api.clock()) if stamp is None else stamp
    signature = signature or sign(raw, recipient, stamp)
    return (client or api.client).post("/inbound", content=raw, headers={
        "Content-Type": "message/rfc822", "X-Lacre-Signature": signature,
        "X-Lacre-Recipient": recipient, "X-Lacre-Timestamp": str(stamp)})


def counts(api, box):
    body = api.client.get("/mailboxes/%s" % (box["id"],), headers=AUTH).json()
    return body["received"], body["dropped"]


# ---- CRUD and ownership ------------------------------------------------------

def test_a_mailbox_is_created_with_a_random_address(api):
    box = new_mailbox(api)
    assert re.match(r"^[a-z2-7]{12}$", box["id"])
    assert box["address"] == "lacre-%s@in-sidr.xyz" % (box["id"],)
    assert box["enabled"] is True and box["extract"] == "none"
    assert (box["received"], box["dropped"], box["last_received_at"]) == (0, 0, None)
    assert box["jobs"] == "/mailboxes/%s/jobs" % (box["id"],)
    assert new_mailbox(api)["id"] != box["id"]


def test_the_extract_mode_is_the_form_field_or_the_default(api):
    assert new_mailbox(api, extract="LLM")["extract"] == "llm"
    response = api.client.post("/mailboxes", headers=AUTH, data={"extract": "regex"})
    assert response.status_code == 422


def test_the_mail_domain_is_configurable(api):
    settings = support.settings(api.settings.data_dir, mail_domain="mail.example.org")
    client = TestClient(create_app(settings, api.store, api.blobs, api.contracts,
                                   clock=api.clock))
    box = client.post("/mailboxes", headers=AUTH).json()
    assert box["address"].endswith("@mail.example.org")


def test_list_get_and_disable(api):
    first, second = new_mailbox(api), new_mailbox(api)
    listed = api.client.get("/mailboxes", headers=AUTH).json()["mailboxes"]
    assert [b["id"] for b in listed] == [first["id"], second["id"]]
    assert api.client.get("/mailboxes/%s" % (first["id"],), headers=AUTH).json() == first

    response = api.client.delete("/mailboxes/%s" % (first["id"],), headers=AUTH)
    assert response.status_code == 200
    assert response.json()["enabled"] is False and response.json()["disabled_at"]
    # Disabled, not gone.
    again = api.client.get("/mailboxes/%s" % (first["id"],), headers=AUTH).json()
    assert again["enabled"] is False
    assert api.client.delete("/mailboxes/%s" % (first["id"],), headers=AUTH).status_code == 200


def test_a_key_never_sees_another_keys_mailboxes(api):
    mine = new_mailbox(api)
    theirs = new_mailbox(api, auth=OTHER)
    assert [b["id"] for b in api.client.get("/mailboxes", headers=AUTH).json()["mailboxes"]] \
        == [mine["id"]]
    assert [b["id"] for b in api.client.get("/mailboxes", headers=OTHER).json()["mailboxes"]] \
        == [theirs["id"]]
    for method in ("get", "delete"):
        for path in ("/mailboxes/%s", "/mailboxes/%s/jobs"):
            if method == "delete" and path.endswith("jobs"):
                continue
            response = getattr(api.client, method)(path % (theirs["id"],), headers=AUTH)
            # The same answer as for an id that does not exist.
            assert response.status_code == 404
            assert response.json() == {"detail": "no such mailbox"}
    assert api.store.mailbox(theirs["id"])["enabled"] == 1


def test_the_key_itself_is_never_stored(api):
    new_mailbox(api)
    dump = "\n".join(api.store._db.iterdump())
    assert API_KEY not in dump
    assert hashlib.sha256(API_KEY.encode()).hexdigest() in dump


@pytest.mark.parametrize("method,path", [
    ("post", "/mailboxes"), ("get", "/mailboxes"), ("get", "/mailboxes/abcdefghijkl"),
    ("delete", "/mailboxes/abcdefghijkl"), ("get", "/mailboxes/abcdefghijkl/jobs")])
def test_mailbox_endpoints_need_the_key(api, method, path):
    response = getattr(api.client, method)(path, headers={"X-API-Key": "wrong-" + "y" * 30})
    assert response.status_code == 401


def test_a_malformed_id_is_404(api):
    assert api.client.get("/mailboxes/ABC", headers=AUTH).status_code == 404
    assert api.client.get("/mailboxes/abcdefghijk1", headers=AUTH).status_code == 404


# ---- inbound -----------------------------------------------------------------

def test_a_signed_delivery_creates_a_job_for_the_owner(api):
    box = new_mailbox(api, extract="auto")
    api.clock.advance(30)
    response = deliver(api, box["address"])
    assert response.status_code == 202, response.text
    stub = response.json()
    assert set(stub) == {"job_id", "status", "job", "extract"}
    assert stub["status"] == "pending" and stub["extract"] == "auto"
    job = api.client.get(stub["job"], headers=AUTH).json()
    assert (job["via"], job["mailbox"]) == ("inbound", box["id"])
    assert job["extraction"]["requested"] == "auto"
    owner = api.store.account_by_key(hashlib.sha256(API_KEY.encode()).hexdigest())
    assert api.store.job(stub["job_id"])["account_id"] == owner["id"]
    body = api.client.get("/mailboxes/%s" % (box["id"],), headers=AUTH).json()
    assert (body["received"], body["dropped"]) == (1, 0)
    assert body["last_received_at"] == "2026-09-21T14:13:50Z"
    # Staged exactly as an upload would be: signed headers, and the body
    # because the mailbox extracts.
    assert api.blobs.is_staged(stub["job_id"]) and api.bodies.is_staged(stub["job_id"])


def test_an_uploaded_job_says_it_came_through_the_api(api):
    job_id = api.client.post("/attest", headers=AUTH, files={
        "eml": ("m.eml", support.amazon_eml(), "message/rfc822")}).json()["job_id"]
    job = api.client.get("/jobs/%s" % (job_id,), headers=AUTH).json()
    assert (job["via"], job["mailbox"]) == ("api", None)


def test_the_recipient_is_matched_without_case(api):
    box = new_mailbox(api)
    assert deliver(api, box["address"].upper()).status_code == 202


def test_a_bad_signature_is_refused(api):
    box = new_mailbox(api)
    raw = support.amazon_eml()
    stamp = int(api.clock())
    cases = [
        sign(raw, box["address"], stamp, secret="wrong-" + "s" * 40),
        # Signed for another mailbox.
        sign(raw, "lacre-aaaaaaaaaaaa@in-sidr.xyz", stamp),
        # Signed with another timestamp.
        sign(raw, box["address"], stamp - 1),
        # Signed over other bytes.
        sign(raw + b"x", box["address"], stamp),
        "not hex",
    ]
    for signature in cases:
        response = deliver(api, box["address"], raw, stamp, signature=signature)
        assert response.status_code == 401
    response = api.client.post("/inbound", content=raw, headers=AUTH)
    assert response.status_code == 401
    assert api.store.open_jobs() == []
    assert counts(api, box) == (0, 0)


@pytest.mark.parametrize("offset", [-301, 301, -3600])
def test_a_stale_or_future_timestamp_is_refused(api, offset):
    box = new_mailbox(api)
    response = deliver(api, box["address"], stamp=int(api.clock()) + offset)
    assert response.status_code == 401
    assert "window" in response.json()["detail"]
    assert api.store.open_jobs() == []


@pytest.mark.parametrize("offset", [-300, 300])
def test_a_timestamp_at_the_edge_of_the_window_is_taken(api, offset):
    box = new_mailbox(api)
    assert deliver(api, box["address"], stamp=int(api.clock()) + offset).status_code == 202


def test_a_replayed_delivery_is_refused(api):
    box = new_mailbox(api)
    raw = support.amazon_eml()
    stamp = int(api.clock())
    assert deliver(api, box["address"], raw, stamp).status_code == 202
    api.clock.advance(120)
    response = deliver(api, box["address"], raw, stamp)
    assert response.status_code == 409
    assert len(api.store.open_jobs()) == 1
    # Once the timestamp is out of the window, the replay fails on it
    # instead, whether or not the signature is still remembered.
    api.clock.advance(600)
    assert deliver(api, box["address"], raw, stamp).status_code == 401
    assert counts(api, box) == (1, 0)


def test_the_same_message_signed_again_is_a_new_delivery(api):
    box = new_mailbox(api)
    assert deliver(api, box["address"]).status_code == 202
    api.clock.advance(1)
    assert deliver(api, box["address"]).status_code == 202
    assert counts(api, box) == (2, 0)


@pytest.mark.parametrize("recipient", [
    "lacre-aaaaaaaaaaaa@in-sidr.xyz", "lacre-short@in-sidr.xyz",
    "someone@in-sidr.xyz", "lacre-aaaaaaaaaaaa@example.org"])
def test_mail_to_an_unknown_mailbox_is_dropped_and_counted(api, recipient):
    response = deliver(api, recipient)
    assert response.status_code == 404
    assert api.store.open_jobs() == []
    assert list(api.blobs.staged.iterdir()) == [] and list(api.bodies.staged.iterdir()) == []
    assert api.store.unknown_inbound() == 1
    assert api.client.get("/health", headers=AUTH).json()["inbound_unknown_dropped"] == 1


def test_mail_to_a_disabled_mailbox_is_dropped_and_counted(api):
    box = new_mailbox(api, extract="auto")
    api.client.delete("/mailboxes/%s" % (box["id"],), headers=AUTH)
    response = deliver(api, box["address"])
    assert response.status_code == 404
    assert api.store.open_jobs() == []
    assert list(api.blobs.staged.iterdir()) == [] and list(api.bodies.staged.iterdir()) == []
    assert counts(api, box) == (0, 1)
    assert api.store.unknown_inbound() == 0


def test_mail_that_cannot_be_attested_is_dropped_and_counted(api):
    box = new_mailbox(api)
    raw = support.amazon_eml().replace(b"DKIM-Signature:", b"X-Old:")
    response = deliver(api, box["address"], raw)
    assert response.status_code == 422
    assert api.store.open_jobs() == []
    assert counts(api, box) == (0, 1)


def test_an_oversized_delivery_is_413(api):
    settings = support.settings(api.settings.data_dir, max_eml_bytes=100)
    client = TestClient(create_app(settings, api.store, api.blobs, api.contracts,
                                   clock=api.clock))
    box = new_mailbox(api)
    raw = support.amazon_eml()
    assert len(raw) > 100
    response = deliver(api, box["address"], raw, client=client)
    assert response.status_code == 413
    assert api.store.open_jobs() == []


def test_the_size_cap_holds_without_a_content_length(api):
    settings = support.settings(api.settings.data_dir, max_eml_bytes=100)
    client = TestClient(create_app(settings, api.store, api.blobs, api.contracts,
                                   clock=api.clock))
    box = new_mailbox(api)
    raw = support.amazon_eml()
    stamp = int(api.clock())

    def chunks():
        for i in range(0, len(raw), 64):
            yield raw[i:i + 64]

    response = client.post("/inbound", content=chunks(), headers={
        "X-Lacre-Signature": sign(raw, box["address"], stamp),
        "X-Lacre-Recipient": box["address"], "X-Lacre-Timestamp": str(stamp)})
    assert response.status_code == 413


def test_inbound_is_off_without_a_secret(api):
    settings = support.settings(api.settings.data_dir, inbound_secret="")
    client = TestClient(create_app(settings, api.store, api.blobs, api.contracts,
                                   clock=api.clock))
    box = new_mailbox(api)
    response = deliver(api, box["address"], client=client,
                       signature=sign(support.amazon_eml(), box["address"], int(api.clock()),
                                      secret=""))
    assert response.status_code == 503


def test_the_mac_helper_is_what_the_tests_sign_with(api):
    raw = b"Subject: x\r\n\r\nbody"
    assert inbound_mac(INBOUND_SECRET, "1790000000", "lacre-a@b.c", raw) \
        == sign(raw, "lacre-a@b.c", 1790000000)


# ---- the jobs of a mailbox ----------------------------------------------------

def test_a_mailbox_lists_its_own_jobs_newest_first_in_pages(api):
    box = new_mailbox(api)
    other = new_mailbox(api)
    ids = []
    for _ in range(5):
        api.clock.advance(1)
        ids.append(deliver(api, box["address"]).json()["job_id"])
    deliver(api, other["address"])
    api.client.post("/attest", headers=AUTH, files={
        "eml": ("m.eml", support.amazon_eml(), "message/rfc822")})

    path = "/mailboxes/%s/jobs" % (box["id"],)
    page = api.client.get(path + "?limit=2", headers=AUTH).json()
    assert [j["id"] for j in page["jobs"]] == ids[::-1][:2]
    assert page["next"] == path + "?limit=2&offset=2"
    seen = [j["id"] for j in page["jobs"]]
    while page["next"]:
        page = api.client.get(page["next"], headers=AUTH).json()
        seen += [j["id"] for j in page["jobs"]]
    assert seen == ids[::-1]
    assert all(j["mailbox"] == box["id"] for j in
               api.client.get(path, headers=AUTH).json()["jobs"])
    assert api.client.get(path + "?limit=0", headers=AUTH).status_code == 422
    assert api.client.get(path + "?limit=101", headers=AUTH).status_code == 422


def test_jobs_created_in_the_same_second_keep_a_stable_order(api):
    box = new_mailbox(api)
    raw = support.amazon_eml()
    ids = []
    for i in range(3):
        # Distinct bytes, so distinct signatures, all in one clock tick.
        ids.append(deliver(api, box["address"], raw + b" " * i).json()["job_id"])
    listed = api.client.get("/mailboxes/%s/jobs" % (box["id"],), headers=AUTH).json()
    assert [j["id"] for j in listed["jobs"]] == ids[::-1]


# ---- the same worker path ------------------------------------------------------

def drive(api, job_id):
    """Take a job from pending to its end over FakeChain: (stages, sends)."""
    seen, start = [], len(api.chain.sent)
    job = api.store.job(job_id)
    for _ in range(12):
        api.worker.tick()
        job = api.store.job(job_id)
        seen.append(job["stage"])
        tx_id = job["current_tx"] or job["ext_current_tx"]
        if tx_id and api.chain.states[tx_id] == [support.PENDING]:
            api.chain.script(tx_id, AGREE)
            if job["status"] == "attesting":
                chosen = type("C", (), {"domain": KEY[0], "selector": KEY[1], "bh": job["bh"]})
                api.chain.on_final[tx_id] = lambda c=chosen: support.our_record(api, c)
            else:
                api.chain.extract_outcome(tx_id, "record")
        if job["status"] in ("finalized", "refused", "failed"):
            break
    return seen, [(to, method) for _, to, method, _, _ in api.chain.sent[start:]]


def test_an_inbound_job_takes_the_same_worker_path_as_an_upload(api):
    api.chain.keys[KEY] = support.active_key()
    uploaded = api.client.post("/attest", headers=AUTH, data={"extract": "auto"}, files={
        "eml": ("m.eml", support.amazon_eml(), "message/rfc822")}).json()["job_id"]
    upload_path = drive(api, uploaded)

    box = new_mailbox(api, extract="auto")
    inbound = deliver(api, box["address"]).json()["job_id"]
    inbound_path = drive(api, inbound)

    assert inbound_path == upload_path
    assert upload_path[1] == [(VERIFIER, "attest"), (EXTRACTOR, "extract")]
    job = api.client.get("/jobs/%s" % (inbound,), headers=AUTH).json()
    assert job["status"] == "finalized" and job["valid_and_aligned"] is True
    assert job["extraction"]["status"] == "extracted"
    assert (job["via"], job["mailbox"]) == ("inbound", box["id"])
    # The served body was the message's, byte-exact, and is gone now.
    extract_args = api.chain.sends("extract")[-1][3]
    assert extract_args[0] == job["record_id"]
    assert not api.bodies.is_staged(inbound) and list(api.bodies.served.iterdir()) == []


# ---- migration -----------------------------------------------------------------

def test_a_database_from_before_mailboxes_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite3"
    Store(path)
    db = sqlite3.connect(str(path))
    # Take the store back to its pre-mailbox shape.
    db.execute("DROP TABLE mailboxes")
    db.execute("DROP TABLE inbound_seen")
    db.execute("DROP INDEX jobs_mailbox")
    for column in ("via", "mailbox"):
        db.execute("ALTER TABLE jobs DROP COLUMN %s" % (column,))
    db.execute("INSERT INTO senders (id, domain, selector, updated_at) "
               "VALUES (1, 'amazon.com', 's', 0)")
    db.execute("INSERT INTO jobs (id, status, stage, sender_id, headers_sha256, bh, "
               "created_at, updated_at) VALUES ('old', 'finalized', 'recorded', 1, 'h', 'b', 0, 0)")
    db.commit()
    db.close()

    store = Store(path)
    job = store.job("old")
    assert (job["via"], job["mailbox"], job["account_id"]) == (None, None, None)
    box = store.create_mailbox("a" * 12, "none")
    assert store.mailbox(box["id"])["enabled"] == 1
