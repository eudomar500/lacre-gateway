"""Accounts, metering and top-ups, through a test client over FakeChain.

Each terminal path of a job is taken on a metered account, and its ledger
is read back: the hold when the job is created, then the settle row and,
for whatever wrote no record, the release.
"""

import hashlib
import json
import sqlite3
from types import SimpleNamespace

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

import support
from support import AGREE, API_KEY, UNDERPAID, VERIFIER, NothingSent
from lacre_gateway import accounts
from lacre_gateway.app import create_app
from lacre_gateway.store import LedgerError, Store
from lacre_gateway.topups import SOURCES, ManualTopUp, TopUpSource
from lacre_mcp.gateway import Gateway
from lacre_mcp.server import build_server
from test_mailboxes import deliver
from test_mcp import call

KEY = ("amazon.com", "synthsel2026a")
ADMIN_TOKEN = "test-admin-" + "t" * 32
ADMIN = {"X-Admin-Token": ADMIN_TOKEN}
AUTH = {"X-API-Key": API_KEY}


def app_for(gw, **changes):
    gw.settings = support.settings(gw.settings.data_dir, **dict({"admin_token": ADMIN_TOKEN},
                                                                 **changes))
    gw.app = create_app(gw.settings, gw.store, gw.blobs, gw.contracts, clock=gw.clock,
                        bodies=gw.bodies)
    gw.client = TestClient(gw.app)
    return gw


@pytest.fixture
def api(gw):
    gw.chain.keys[KEY] = support.active_key()
    return app_for(gw)


def customer(api, credits=10, name="acme"):
    """(auth headers, account id) of a new admin-made account."""
    response = api.client.post("/admin/accounts", headers=ADMIN,
                               json={"name": name, "credits": credits})
    assert response.status_code == 201, response.text
    body = response.json()
    return {"X-API-Key": body["api_key"]}, body["account"]["id"]


def upload(api, auth, extract="none", raw=None):
    return api.client.post("/attest", headers=auth, data={"extract": extract}, files={
        "eml": ("m.eml", raw or support.amazon_eml(), "message/rfc822")})


def balance(api, account_id):
    return api.store.account(account_id)["credits"]


def kinds(api, job_id):
    return [(e["kind"], e["delta"]) for e in api.store.job_ledger(job_id)]


def cost(api, job_id, auth):
    return api.client.get("/jobs/%s" % (job_id,), headers=auth).json()["cost"]


def attest(api, job_id, **record):
    """Send the job's attest call and finalize it writing a record."""
    api.worker.tick()
    tx_id = api.chain.sends("attest")[-1][0]
    api.chain.script(tx_id, AGREE)
    j = api.store.job(job_id)
    chosen = SimpleNamespace(domain=KEY[0], selector=KEY[1], bh=j["bh"])
    api.chain.on_final[tx_id] = lambda: support.our_record(api, chosen, **record)
    api.worker.tick()


def extract(api, outcome, state=AGREE, **fields):
    """Serve the body, send extract, and finalize it with outcome."""
    api.worker.tick()
    api.worker.tick()
    tx_id = api.chain.sends("extract")[-1][0]
    api.chain.script(tx_id, state)
    api.chain.extract_outcome(tx_id, outcome, **fields)
    api.worker.tick()


# ---- hold, settle, release on every terminal path ------------------------------

def test_an_attestation_holds_then_charges_attest(api):
    auth, account = customer(api, credits=10)
    job_id = upload(api, auth).json()["job_id"]
    assert balance(api, account) == 9
    assert cost(api, job_id, auth) == {"held": 1, "charged": 0, "released": 0}
    assert kinds(api, job_id) == [("hold", -1)]
    attest(api, job_id)
    assert api.store.job(job_id)["status"] == "finalized"
    assert cost(api, job_id, auth) == {"held": 1, "charged": 1, "released": 0}
    assert kinds(api, job_id) == [("hold", -1), ("settle", 0)]
    assert balance(api, account) == 9


def test_the_account_moves_with_every_job_event(api):
    # The web panel reads /account again on each change of a job; each
    # read has to show that change, from the hold to the charge.
    auth, account = customer(api, credits=10)

    def read():
        return api.client.get("/account", headers=auth).json()

    before = read()
    assert (before["credits"], before["held"], before["counts"]["open"]) == (10, 0, 0)
    job_id = upload(api, auth, extract="patterns").json()["job_id"]
    queued = read()
    assert (queued["credits"], queued["held"], queued["counts"]["open"]) == (8, 2, 1)
    attest(api, job_id)
    job = api.client.get("/jobs/%s" % (job_id,), headers=auth).json()
    assert job["status"] == "extracting" and job["consensus_tx"]
    assert read()["counts"]["open"] == 1
    extract(api, "record")
    job = api.client.get("/jobs/%s" % (job_id,), headers=auth).json()
    # The card shows both transactions, each with its explorer link.
    assert job["extraction"]["consensus_tx"] and job["extraction"]["consensus_tx"] != job["consensus_tx"]
    assert job["extraction"]["consensus_txs"][-1]["explorer"].endswith(job["extraction"]["consensus_tx"])
    done = read()
    assert (done["credits"], done["held"], done["charged"]) == (8, 0, 2)
    assert (done["counts"]["open"], done["counts"]["finalized"]) == (0, 1)


def test_an_invalid_verifier_record_still_charges_attest(api):
    auth, account = customer(api)
    job_id = upload(api, auth).json()["job_id"]
    attest(api, job_id, valid=False)
    assert cost(api, job_id, auth) == {"held": 1, "charged": 1, "released": 0}


def test_an_extraction_record_charges_both(api):
    auth, account = customer(api)
    job_id = upload(api, auth, extract="patterns").json()["job_id"]
    assert balance(api, account) == 8
    attest(api, job_id)
    assert api.store.job(job_id)["status"] == "extracting"
    extract(api, "record")
    assert api.store.job(job_id)["ext_status"] == "extracted"
    assert cost(api, job_id, auth) == {"held": 2, "charged": 2, "released": 0}
    assert kinds(api, job_id) == [("hold", -2), ("settle", 0)]
    assert balance(api, account) == 8


def test_a_match_false_record_charges_extract_too(api):
    auth, account = customer(api)
    job_id = upload(api, auth, extract="patterns").json()["job_id"]
    attest(api, job_id)
    extract(api, "record", match=False, reason="body HTTP 404")
    assert api.store.job(job_id)["ext_status"] == "no match"
    assert cost(api, job_id, auth) == {"held": 2, "charged": 2, "released": 0}
    assert balance(api, account) == 8


def test_an_extraction_refused_on_chain_releases_extract(api):
    auth, account = customer(api)
    job_id = upload(api, auth, extract="patterns").json()["job_id"]
    attest(api, job_id)
    extract(api, "refuse", state=UNDERPAID, reason="record not found")
    assert api.store.job(job_id)["ext_status"] == "refused"
    assert cost(api, job_id, auth) == {"held": 2, "charged": 1, "released": 1}
    assert kinds(api, job_id) == [("hold", -2), ("settle", 0), ("release", 1)]
    assert balance(api, account) == 9


def test_an_extraction_refused_before_sending_releases_extract(api):
    auth, account = customer(api)
    api.chain.patterns.clear()
    job_id = upload(api, auth, extract="patterns").json()["job_id"]
    attest(api, job_id)
    api.worker.tick()
    api.worker.tick()
    assert api.store.job(job_id)["ext_status"] == "refused"
    assert api.chain.sends("extract") == []
    assert cost(api, job_id, auth) == {"held": 2, "charged": 1, "released": 1}
    assert balance(api, account) == 9


def test_an_extraction_skipped_after_the_record_releases_extract(api):
    auth, account = customer(api)
    job_id = upload(api, auth, extract="patterns").json()["job_id"]
    attest(api, job_id, valid=False)
    assert api.store.job(job_id)["ext_status"] == "skipped"
    assert cost(api, job_id, auth) == {"held": 2, "charged": 1, "released": 1}
    assert balance(api, account) == 9


def test_an_extraction_that_cannot_run_is_not_held(api):
    auth, account = customer(api)
    raw = support.amazon_eml().replace(b"Arriving: Thursday", b"Arriving: Friday!")
    job_id = upload(api, auth, extract="patterns", raw=raw).json()["job_id"]
    assert api.store.job(job_id)["ext_status"] == "skipped"
    assert cost(api, job_id, auth)["held"] == 1 and balance(api, account) == 9


def test_a_refused_attestation_releases_everything(api):
    auth, account = customer(api)
    job_id = upload(api, auth, extract="patterns").json()["job_id"]
    api.worker.tick()
    api.chain.script(api.chain.sends("attest")[-1][0], AGREE)
    api.chain.refusal[VERIFIER] = "key pending"
    api.worker.tick()
    assert api.store.job(job_id)["status"] == "refused"
    assert cost(api, job_id, auth) == {"held": 2, "charged": 0, "released": 2}
    assert kinds(api, job_id) == [("hold", -2), ("settle", 0), ("release", 2)]
    assert balance(api, account) == 10


def test_a_job_refused_before_sending_releases_everything(api):
    auth, account = customer(api)
    api.chain.keys[KEY] = dict(support.active_key(), state="retired")
    job_id = upload(api, auth).json()["job_id"]
    api.worker.tick()
    assert api.store.job(job_id)["status"] == "refused"
    assert cost(api, job_id, auth) == {"held": 1, "charged": 0, "released": 1}
    assert balance(api, account) == 10


def test_a_failed_job_releases_everything(api):
    auth, account = customer(api)
    api.chain.fail_next_send = [NothingSent("rpc down")] * 3
    job_id = upload(api, auth).json()["job_id"]
    for _ in range(3):
        api.worker.tick()
    assert api.store.job(job_id)["status"] == "failed"
    assert cost(api, job_id, auth) == {"held": 1, "charged": 0, "released": 1}
    assert balance(api, account) == 10


def test_a_message_that_could_not_be_stored_releases_its_hold(api, monkeypatch):
    auth, account = customer(api)

    def broken(*args):
        raise OSError("disk full")
    monkeypatch.setattr(api.blobs, "stage", broken)
    assert upload(api, auth).status_code == 500
    assert balance(api, account) == 10
    [row] = api.store._all("SELECT id, status FROM jobs")
    assert row["status"] == "failed"
    assert kinds(api, row["id"]) == [("hold", -1), ("settle", 0), ("release", 1)]


def test_a_job_settles_once(api):
    auth, account = customer(api)
    job_id = upload(api, auth).json()["job_id"]
    api.store.update_job(job_id, status="failed", stage="stopped", error="x")
    api.store.update_job(job_id, status="failed", stage="stopped", error="again")
    assert kinds(api, job_id) == [("hold", -1), ("settle", 0), ("release", 1)]
    assert balance(api, account) == 10


# ---- 402 ---------------------------------------------------------------------------

def test_an_empty_balance_is_402_with_the_shortfall(api):
    auth, account = customer(api, credits=0)
    response = upload(api, auth)
    assert response.status_code == 402
    assert response.json() == {
        "detail": "not enough credits: this job holds 1, the balance is 0, 1 short",
        "needed": 1, "credits": 0, "shortfall": 1}
    assert api.store._all("SELECT id FROM jobs") == []
    assert list(api.blobs.staged.iterdir()) == [] and list(api.bodies.staged.iterdir()) == []
    assert api.store.ledger(account, 10) == []


def test_the_extraction_counts_toward_the_hold(api):
    auth, account = customer(api, credits=1)
    response = upload(api, auth, extract="patterns")
    assert response.status_code == 402 and response.json()["shortfall"] == 1
    assert upload(api, auth, extract="none").status_code == 202
    assert balance(api, account) == 0


def test_inbound_mail_on_an_empty_balance_is_dropped_and_counted(api):
    auth, account = customer(api, credits=0)
    box = api.client.post("/mailboxes", headers=auth, data={"extract": "auto"}).json()
    response = deliver(api, box["address"])
    assert response.status_code == 402
    body = api.client.get("/mailboxes/%s" % (box["id"],), headers=auth).json()
    assert (body["received"], body["dropped"]) == (0, 1)
    assert api.store._all("SELECT id FROM jobs") == []
    assert list(api.blobs.staged.iterdir()) == [] and list(api.bodies.staged.iterdir()) == []


def test_inbound_mail_to_a_disabled_account_is_dropped(api):
    auth, account = customer(api)
    box = api.client.post("/mailboxes", headers=auth).json()
    api.client.post("/admin/accounts/%s/disable" % (account,), headers=ADMIN)
    assert deliver(api, box["address"]).status_code == 404
    assert api.store.mailbox(box["id"])["dropped"] == 1


# ---- bootstrap accounts ----------------------------------------------------------

def test_a_bootstrap_key_is_an_unlimited_account(api):
    body = api.client.get("/account", headers=AUTH).json()
    assert body["name"] == "bootstrap-1" and body["unlimited"] is True
    assert body["credits"] == 0 and body["prices"] == {"attest": 1, "extract": 1}
    job_id = upload(api, AUTH, extract="patterns").json()["job_id"]
    attest(api, job_id)
    extract(api, "record")
    for _ in range(2):
        upload(api, AUTH, extract="patterns")
    # Priced and settled like any job, and nothing taken from a balance.
    assert cost(api, job_id, AUTH) == {"held": 2, "charged": 2, "released": 0}
    assert api.store.ledger(body["id"], 10) == []
    after = api.client.get("/account", headers=AUTH).json()
    assert after["credits"] == 0
    assert after["counts"] == {"jobs": 3, "open": 2, "finalized": 1, "refused": 0,
                               "failed": 0, "mailboxes": 0}
    assert (after["held"], after["charged"]) == (4, 2)


def test_bootstrap_credits_make_bootstrap_accounts_metered(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    accounts.bootstrap(store, (API_KEY,), 5)
    [account] = store.accounts()
    assert (account["name"], account["credits"], account["unlimited"]) == ("bootstrap-1", 5, 0)
    assert [(e["kind"], e["delta"]) for e in store.ledger(account["id"], 10)] == [("adjust", 5)]
    # Every start follows the configuration, and never grants twice.
    accounts.bootstrap(store, (API_KEY,), 0)
    accounts.bootstrap(store, (API_KEY,), 0)
    [account] = store.accounts()
    assert (account["credits"], account["unlimited"]) == (5, 1)


def test_bootstrap_is_idempotent_and_numbers_new_entries(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    second = "second-key-" + "q" * 24
    accounts.bootstrap(store, (API_KEY,), 0)
    accounts.bootstrap(store, (API_KEY, second), 0)
    accounts.bootstrap(store, (API_KEY, second), 0)
    assert [a["name"] for a in store.accounts()] == ["bootstrap-1", "bootstrap-2"]


def test_a_key_taken_out_of_the_list_is_revoked(api):
    other = "other-key-" + "z" * 24
    app_for(api, api_keys=(API_KEY, other))
    assert api.client.get("/account", headers={"X-API-Key": other}).status_code == 200
    app_for(api, api_keys=(API_KEY,))
    response = api.client.get("/account", headers={"X-API-Key": other})
    assert response.status_code == 403 and response.json()["detail"] == "this account is disabled"


# ---- who sees a job ----------------------------------------------------------------

MISSING = {"detail": "no such job"}


def test_an_uploaded_job_is_seen_by_its_account_only(api):
    mine, _ = customer(api, name="mine")
    theirs, _ = customer(api, name="theirs")
    job_id = upload(api, mine).json()["job_id"]
    assert api.client.get("/jobs/%s" % (job_id,), headers=mine).status_code == 200
    for other in (theirs, AUTH):
        response = api.client.get("/jobs/%s" % (job_id,), headers=other)
        # The same answer as for a job id that does not exist.
        assert (response.status_code, response.json()) == (404, MISSING)
    missing = api.client.get("/jobs/%s" % ("0" * 32,), headers=mine)
    assert (missing.status_code, missing.json()) == (404, MISSING)


def test_a_bootstrap_accounts_job_is_hidden_from_other_accounts(api):
    theirs, _ = customer(api)
    job_id = upload(api, AUTH).json()["job_id"]
    assert api.client.get("/jobs/%s" % (job_id,), headers=AUTH).status_code == 200
    assert api.client.get("/jobs/%s" % (job_id,), headers=theirs).json() == MISSING
    other = "other-key-" + "z" * 24
    app_for(api, api_keys=(API_KEY, other))
    response = api.client.get("/jobs/%s" % (job_id,), headers={"X-API-Key": other})
    assert (response.status_code, response.json()) == (404, MISSING)


def test_an_inbound_job_is_seen_by_the_mailboxs_account_only(api):
    mine, _ = customer(api, name="mine")
    theirs, _ = customer(api, name="theirs")
    box = api.client.post("/mailboxes", headers=mine).json()
    job_id = deliver(api, box["address"]).json()["job_id"]
    body = api.client.get("/jobs/%s" % (job_id,), headers=mine).json()
    assert (body["id"], body["via"], body["mailbox"]) == (job_id, "inbound", box["id"])
    for other in (theirs, AUTH):
        response = api.client.get("/jobs/%s" % (job_id,), headers=other)
        assert (response.status_code, response.json()) == (404, MISSING)


def test_a_rotated_key_still_sees_the_accounts_jobs(api):
    auth, _ = customer(api)
    job_id = upload(api, auth).json()["job_id"]
    fresh = {"X-API-Key": api.client.post("/account/rotate-key", headers=auth).json()["api_key"]}
    assert api.client.get("/jobs/%s" % (job_id,), headers=fresh).status_code == 200


def test_records_stay_readable_by_any_key(api):
    mine, _ = customer(api, name="mine")
    theirs, _ = customer(api, name="theirs")
    job_id = upload(api, mine, extract="patterns").json()["job_id"]
    attest(api, job_id)
    extract(api, "record")
    job = api.client.get("/jobs/%s" % (job_id,), headers=mine).json()
    assert api.client.get("/jobs/%s" % (job_id,), headers=theirs).status_code == 404
    record = api.client.get("/records/%s" % (job["record_id"],), headers=theirs,
                            params={"verifier": job["verifier"]})
    assert record.status_code == 200
    extraction = api.client.get("/extractions/patterns/%s"
                                % (job["extraction"]["record_id"],), headers=theirs)
    assert extraction.status_code == 200


def test_lacre_job_gives_not_found_for_another_accounts_job(api):
    mine, _ = customer(api, name="mine")
    theirs, _ = customer(api, name="theirs")
    job_id = upload(api, mine).json()["job_id"]
    transport = httpx.ASGITransport(app=api.app, raise_app_exceptions=False)
    for auth, expected in ((mine, False), (theirs, True)):
        server = build_server(lambda ctx, key=auth["X-API-Key"]: Gateway(
            "http://gateway", key, transport=transport))
        error, answer = call(server, "lacre_job", {"job_id": job_id})
        assert error is expected
        if error:
            assert "404" in answer and "no such job" in answer
        else:
            assert answer["id"] == job_id


# ---- key rotation ------------------------------------------------------------------

def test_rotating_a_key_keeps_the_account(api):
    auth, account = customer(api)
    box = api.client.post("/mailboxes", headers=auth).json()
    job_id = upload(api, auth).json()["job_id"]
    response = api.client.post("/account/rotate-key", headers=auth)
    assert response.status_code == 200
    body = response.json()
    fresh = {"X-API-Key": body["api_key"]}
    assert body["account"]["id"] == account and body["account"]["credits"] == 9
    assert api.client.get("/account", headers=auth).status_code == 401
    assert [b["id"] for b in api.client.get("/mailboxes", headers=fresh).json()["mailboxes"]] \
        == [box["id"]]
    assert api.store.job(job_id)["account_id"] == account
    assert body["api_key"] not in "\n".join(api.store._db.iterdump())


def test_a_rotated_bootstrap_key_is_not_bootstrapped_again(api):
    response = api.client.post("/account/rotate-key", headers=AUTH).json()
    fresh = {"X-API-Key": response["api_key"]}
    app_for(api)
    assert len(api.store.accounts()) == 1
    assert api.client.get("/account", headers=AUTH).status_code == 401
    body = api.client.get("/account", headers=fresh).json()
    assert body["unlimited"] is True and body["enabled"] is True
    # Out of the list, a rotated account is not revoked; it only stops
    # being unlimited.
    app_for(api, api_keys=("another-key-" + "w" * 24,))
    body = api.client.get("/account", headers=fresh).json()
    assert body["unlimited"] is False and body["enabled"] is True


def test_rotation_over_mcp_is_not_offered(api):
    names = [t.name for t in anyio.run(build_server(lambda ctx: None).list_tools)]
    assert "lacre_account" in names and not [n for n in names if "rotate" in n]


# ---- migration from key digests -------------------------------------------------------

def test_rows_owned_by_a_key_digest_move_to_its_account(tmp_path):
    path = tmp_path / "old.sqlite3"
    Store(path)
    db = sqlite3.connect(str(path))
    # Take the store back to its shape before accounts.
    for table in ("accounts", "ledger", "topups", "mailboxes"):
        db.execute("DROP TABLE %s" % (table,))
    db.execute("DROP INDEX jobs_account")
    for column in ("account_id", "metered", "held_attest", "held_extract", "charged",
                   "released", "settled_at"):
        db.execute("ALTER TABLE jobs DROP COLUMN %s" % (column,))
    db.execute("ALTER TABLE jobs ADD COLUMN owner TEXT")
    db.execute("CREATE TABLE mailboxes (id TEXT PRIMARY KEY, owner TEXT NOT NULL, "
               "extract_mode TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, "
               "received INTEGER NOT NULL DEFAULT 0, dropped INTEGER NOT NULL DEFAULT 0, "
               "last_received_at REAL, created_at REAL NOT NULL, disabled_at REAL)")
    db.execute("CREATE INDEX mailboxes_owner ON mailboxes (owner)")
    mine = hashlib.sha256(API_KEY.encode()).hexdigest()
    gone = hashlib.sha256(b"a key no longer configured").hexdigest()
    db.execute("INSERT INTO senders (id, domain, selector, updated_at) "
               "VALUES (1, 'amazon.com', 's', 0)")
    for job_id, owner in (("a" * 32, mine), ("b" * 32, gone), ("c" * 32, None)):
        db.execute("INSERT INTO jobs (id, status, stage, sender_id, headers_sha256, bh, owner, "
                   "created_at, updated_at) VALUES (?, 'finalized', 'recorded', 1, 'h', 'b', ?, "
                   "0, 0)", (job_id, owner))
    for box_id, owner in (("aaaaaaaaaaaa", mine), ("bbbbbbbbbbbb", gone)):
        db.execute("INSERT INTO mailboxes (id, owner, extract_mode, created_at) "
                   "VALUES (?, ?, 'none', 0)", (box_id, owner))
    db.commit()
    db.close()

    store = Store(path)
    client = TestClient(create_app(support.settings(tmp_path), store, None,
                                   SimpleNamespace(chain=None)))
    account = store.account_by_key(mine)
    assert account["name"] == "bootstrap-1"
    assert [store.job(i)["account_id"] for i in ("a" * 32, "b" * 32, "c" * 32)] == \
        [account["id"], None, None]
    boxes = client.get("/mailboxes", headers=AUTH).json()["mailboxes"]
    assert [b["id"] for b in boxes] == ["aaaaaaaaaaaa"]
    assert store.mailbox("bbbbbbbbbbbb")["account_id"] is None
    for table in ("jobs", "mailboxes"):
        assert "owner" not in store._columns(table)
    # A second start finds nothing left to move.
    assert store.migrate_owners() == {}


# ---- top-ups -------------------------------------------------------------------------

def test_a_manual_top_up_credits_once_per_reference(api):
    auth, account = customer(api, credits=0)
    path = "/admin/accounts/%s/topups" % (account,)
    first = api.client.post(path, headers=ADMIN, json={"credits": 5, "external_ref": "inv-1",
                                                        "note": "bank transfer"})
    assert first.status_code == 201
    topup = first.json()["topup"]
    assert (topup["source"], topup["credits"], topup["external_ref"]) == ("manual", 5, "inv-1")
    again = api.client.post(path, headers=ADMIN, json={"credits": 5, "external_ref": "inv-1"})
    assert again.status_code == 200
    assert again.json()["created"] is False and again.json()["topup"] == topup
    assert balance(api, account) == 5
    other = api.client.post(path, headers=ADMIN, json={"credits": 7, "external_ref": "inv-1"})
    assert other.status_code == 409
    assert [(e["kind"], e["delta"], e["topup_id"]) for e in api.store.ledger(account, 10)] == \
        [("topup", 5, topup["id"])]
    assert upload(api, auth).status_code == 202


def test_a_top_up_needs_a_positive_amount_and_a_reference(api):
    _, account = customer(api)
    path = "/admin/accounts/%s/topups" % (account,)
    for body in ({"credits": 0, "external_ref": "x"}, {"credits": 1, "external_ref": ""},
                 {"credits": 1}):
        assert api.client.post(path, headers=ADMIN, json=body).status_code == 422
    assert api.client.post("/admin/accounts/aaaaaaaaaaaa/topups", headers=ADMIN,
                           json={"credits": 1, "external_ref": "x"}).status_code == 404


def test_the_registry_holds_the_manual_source_only(api):
    assert SOURCES == {"manual": ManualTopUp}
    assert issubclass(ManualTopUp, TopUpSource)
    _, account = customer(api, credits=0)
    row, created = ManualTopUp(api.store).credit(account, 3, "receipt-9")
    assert created and row["source"] == "manual" and balance(api, account) == 3


# ---- the admin API ---------------------------------------------------------------------

ADMIN_CALLS = [("post", "/admin/accounts", {"name": "x"}), ("get", "/admin/accounts", None),
               ("get", "/admin/accounts/aaaaaaaaaaaa", None),
               ("post", "/admin/accounts/aaaaaaaaaaaa/topups",
                {"credits": 1, "external_ref": "r"}),
               ("post", "/admin/accounts/aaaaaaaaaaaa/disable", None),
               ("post", "/admin/accounts/aaaaaaaaaaaa/enable", None)]


@pytest.mark.parametrize("method,path,body", ADMIN_CALLS)
def test_the_admin_api_is_off_without_a_token(gw, method, path, body):
    app_for(gw, admin_token="")
    response = getattr(gw.client, method)(path, headers=ADMIN, **({"json": body} if body else {}))
    assert response.status_code == 503


@pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "wrong-" + "t" * 40}, AUTH,
                                     {"X-Admin-Token": API_KEY}])
@pytest.mark.parametrize("method,path,body", ADMIN_CALLS)
def test_the_admin_api_takes_the_admin_token_only(api, method, path, body, headers):
    response = getattr(api.client, method)(path, headers=headers,
                                           **({"json": body} if body else {}))
    assert response.status_code == 401


def test_admin_reads_accounts_and_their_ledger(api):
    auth, account = customer(api, credits=4)
    job_id = upload(api, auth).json()["job_id"]
    listed = api.client.get("/admin/accounts", headers=ADMIN).json()["accounts"]
    assert [(a["name"], a["bootstrap"]) for a in listed] == [("bootstrap-1", True),
                                                             ("acme", False)]
    body = api.client.get("/admin/accounts/%s" % (account,), headers=ADMIN).json()
    assert body["credits"] == 3 and body["held"] == 1
    assert [(e["kind"], e["delta"], e["job_id"]) for e in body["ledger"]] == \
        [("hold", -1, job_id), ("adjust", 4, None)]
    assert "key_sha256" not in json.dumps(body) and "api_key" not in body


def test_disable_and_enable(api):
    auth, account = customer(api)
    body = api.client.post("/admin/accounts/%s/disable" % (account,), headers=ADMIN).json()
    assert body["enabled"] is False and body["disabled_at"]
    for method, path in (("get", "/account"), ("post", "/mailboxes"), ("get", "/health")):
        assert getattr(api.client, method)(path, headers=auth).status_code == 403
    assert upload(api, auth).status_code == 403
    body = api.client.post("/admin/accounts/%s/enable" % (account,), headers=ADMIN).json()
    assert body["enabled"] is True and body["disabled_at"] is None
    assert api.client.get("/account", headers=auth).status_code == 200


def test_a_new_account_key_is_shown_once_and_stored_as_a_digest(api):
    response = api.client.post("/admin/accounts", headers=ADMIN, json={"name": " acme ",
                                                                        "credits": 2})
    body = response.json()
    assert body["account"]["name"] == "acme" and body["account"]["credits"] == 2
    assert len(body["api_key"]) >= 24
    dump = "\n".join(api.store._db.iterdump())
    assert body["api_key"] not in dump and accounts.digest(body["api_key"]) in dump
    assert api.client.post("/admin/accounts", headers=ADMIN,
                           json={"name": "   "}).status_code == 422


# ---- the ledger check ------------------------------------------------------------------

def test_a_balance_that_disagrees_with_the_ledger_is_reset_on_start(api, caplog):
    auth, account = customer(api, credits=6)
    upload(api, auth)
    api.store._run("UPDATE accounts SET credits = 99 WHERE id = ?", (account,))
    app_for(api)
    assert balance(api, account) == 5
    assert "did not match its ledger" in caplog.text
    assert api.store.check_ledger() == []


def test_a_ledger_that_sums_below_zero_stops_the_start(api):
    _, account = customer(api, credits=1)
    api.store._run("INSERT INTO ledger (account_id, delta, kind, created_at) "
                   "VALUES (?, -5, 'adjust', 0)", (account,))
    with pytest.raises(LedgerError):
        api.store.check_ledger()


# ---- MCP ---------------------------------------------------------------------------

def test_lacre_account_reads_the_callers_account(api):
    auth, account = customer(api, credits=3)
    transport = httpx.ASGITransport(app=api.app, raise_app_exceptions=False)
    server = build_server(lambda ctx: Gateway("http://gateway", auth["X-API-Key"],
                                              transport=transport))
    error, body = call(server, "lacre_account")
    assert not error
    assert (body["id"], body["credits"], body["unlimited"]) == (account, 3, False)
    assert body["prices"] == {"attest": 1, "extract": 1}


def test_a_402_reaches_the_agent_as_a_tool_error(api):
    auth, _ = customer(api, credits=0)
    transport = httpx.ASGITransport(app=api.app, raise_app_exceptions=False)
    server = build_server(lambda ctx: Gateway("http://gateway", auth["X-API-Key"],
                                              transport=transport))
    error, text = call(server, "lacre_attest",
                       {"eml": support.amazon_eml().decode("ascii"), "extract": "none"})
    assert error and "402" in text and "1 short" in text
