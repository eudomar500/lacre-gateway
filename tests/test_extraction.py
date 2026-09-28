"""Extraction after attestation: lane choice, the body, the protocol, the JSON.

The extract call is judged as tools/extract.py judges it: records_of and
last_refusal on the Extractor before the first attempt and after each one,
a record only when one is read back whose record_id and fee_paid are the
call's, a resend only after a finalization without execution. FakeChain
fails any test in which a second call goes to a contract while one of ours
is undecided there.
"""

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import support
from support import (ACCEPTED, AGREE, API_KEY, EXTRACTOR, EXTRACTOR_LLM, PENDING, TIMEOUT,
                     UNDERPAID, VERIFIER)
from lacre_gateway import headers
from lacre_gateway.app import create_app
from lacre_gateway.store import Store

KEY = ("amazon.com", "synthsel2026a")
AUTH = {"X-API-Key": API_KEY}
ORDER = "000-0000000-0000000"


@pytest.fixture
def active(gw):
    gw.chain.keys[KEY] = support.active_key()
    return gw


def job(gw, job_id):
    return gw.store.job(job_id)


def recorded(gw, extract="auto", raw=None, **record):
    """Submit and take the job through its attest record: (job id, chosen)."""
    job_id, chosen = support.submit(gw, raw, extract=extract)
    gw.worker.tick()
    tx_id = gw.chain.sends("attest")[-1][0]
    gw.chain.script(tx_id, AGREE)
    gw.chain.on_final[tx_id] = lambda: support.our_record(gw, chosen, **record)
    gw.worker.tick()
    return job_id, chosen


def last_extract(gw):
    return gw.chain.sends("extract")[-1]


def stages(gw, job_id, ticks):
    seen = []
    for _ in range(ticks):
        gw.worker.tick()
        seen.append(job(gw, job_id)["stage"])
    return seen


# ---- the whole path ----------------------------------------------------------

def test_a_patterns_extraction_from_recorded_to_extracted(active):
    gw = active
    gw.chain.ext_fee = 3
    job_id, chosen = recorded(gw)
    j = job(gw, job_id)
    assert (j["status"], j["stage"]) == ("extracting", "recorded")
    # The attest call is over: its headers are gone, the body is not served yet.
    assert gw.blobs.read(j["blob_token"]) is None
    assert gw.bodies.is_staged(job_id) and list(gw.bodies.served.iterdir()) == []

    gw.worker.tick()
    j = job(gw, job_id)
    assert (j["stage"], j["ext_lane"], j["extractor"]) == ("serving body", "patterns", EXTRACTOR)
    assert len(j["body_name"]) == 32 and int(j["body_name"], 16) >= 0
    assert gw.bodies.read(j["body_name"]) == support.eml_body()
    assert gw.chain.sends("extract") == []

    gw.worker.tick()
    tx_id, to, method, args, value = last_extract(gw)
    assert (to, value) == (EXTRACTOR, 3)
    assert args == ["0", "https://gateway.example.org/b/%s.bin" % (j["body_name"],)]
    assert job(gw, job_id)["stage"] == "extracting"

    gw.chain.script(tx_id, ACCEPTED, AGREE)
    gw.chain.extract_outcome(tx_id, "record")
    assert stages(gw, job_id, 2) == ["waiting for extraction FINALIZED", "extracted"]
    j = job(gw, job_id)
    assert (j["status"], j["ext_status"], j["ext_record_id"]) == ("finalized", "extracted", "0")
    assert j["ext_record"]["shipped"] is True and j["ext_record"]["method"] == "patterns"
    assert "requester" not in j["ext_record"]
    assert gw.bodies.read(j["body_name"]) is None and not gw.bodies.is_staged(job_id)
    assert gw.store.inflight() == []
    # The attest record is untouched.
    assert j["record_id"] == "0" and j["valid_aligned"] == 1


def test_the_body_is_served_byte_exact_after_the_first_crlf_crlf(active):
    gw = active
    job_id, _ = recorded(gw)
    gw.worker.tick()
    served = gw.bodies.read(job(gw, job_id)["body_name"])
    raw = support.amazon_eml()
    assert served == raw.split(b"\r\n\r\n", 1)[1]
    assert served.startswith(b"------=_Part_000") and served.endswith(b"--\r\n")


# ---- the lane --------------------------------------------------------------

def test_auto_takes_the_patterns_lane_when_the_sender_domain_has_patterns(active):
    gw = active
    job_id, _ = recorded(gw, extract="auto")
    gw.worker.tick()
    assert job(gw, job_id)["ext_lane"] == "patterns"
    assert (EXTRACTOR, "patterns", ("amazon.com",), False) in gw.chain.reads


def test_auto_takes_the_llm_lane_when_it_has_none(active):
    gw = active
    gw.chain.patterns.clear()
    job_id, _ = recorded(gw, extract="auto")
    gw.worker.tick()
    gw.worker.tick()
    j = job(gw, job_id)
    assert (j["ext_lane"], j["extractor"]) == ("llm", EXTRACTOR_LLM)
    tx_id = last_extract(gw)[0]
    assert last_extract(gw)[1] == EXTRACTOR_LLM
    gw.chain.script(tx_id, AGREE)
    gw.chain.extract_outcome(tx_id, "record")
    gw.worker.tick()
    j = job(gw, job_id)
    assert j["ext_status"] == "extracted" and j["ext_record"]["flagged"] is False
    assert j["ext_record"]["prompt_sha256"].startswith("820e133d")


def test_auto_takes_the_llm_lane_when_the_router_names_no_pattern_extractor(active):
    gw = active
    del gw.chain.resolves["extractor"]
    job_id, _ = recorded(gw, extract="auto")
    gw.worker.tick()
    assert job(gw, job_id)["ext_lane"] == "llm"


def test_extract_none_ends_at_recorded_as_before(active):
    gw = active
    job_id, _ = recorded(gw, extract="none")
    j = job(gw, job_id)
    assert (j["status"], j["stage"], j["ext_status"]) == ("finalized", "recorded", None)
    assert list(gw.bodies.staged.iterdir()) == []


# ---- refusals and records that end the job ----------------------------------

def test_a_call_the_extractor_would_refuse_is_not_sent(active):
    gw = active
    gw.chain.patterns.clear()
    job_id, _ = recorded(gw, extract="patterns")
    gw.worker.tick()
    gw.worker.tick()
    j = job(gw, job_id)
    assert (j["status"], j["stage"]) == ("finalized", "extraction refused")
    assert (j["ext_status"], j["ext_note"]) == ("refused", "no patterns for domain")
    assert gw.chain.sends("extract") == []
    assert gw.bodies.read(j["body_name"]) is None and gw.store.inflight() == []
    assert j["record_id"] == "0" and j["valid_aligned"] == 1


def test_a_refusal_read_from_last_refusal_ends_the_job(active):
    gw = active
    gw.chain.ext_fee = 5
    job_id, _ = recorded(gw)
    gw.worker.tick()
    gw.worker.tick()
    tx_id = last_extract(gw)[0]
    gw.chain.script(tx_id, UNDERPAID)
    gw.chain.extract_outcome(tx_id, "refuse", reason="record not found")
    gw.worker.tick()
    j = job(gw, job_id)
    assert (j["stage"], j["ext_status"], j["ext_note"]) == \
        ("extraction refused", "refused", "record not found")
    assert len(gw.chain.sends("extract")) == 1
    assert gw.bodies.read(j["body_name"]) is None


def test_a_match_false_record_ends_the_job_and_names_the_reason(active):
    gw = active
    job_id, _ = recorded(gw)
    gw.worker.tick()
    gw.worker.tick()
    tx_id = last_extract(gw)[0]
    gw.chain.script(tx_id, AGREE)
    gw.chain.extract_outcome(tx_id, "record", match=False, reason="body HTTP 404",
                             shipped=False, eta_day="", order_id_found=False)
    gw.worker.tick()
    j = job(gw, job_id)
    assert (j["status"], j["stage"], j["ext_status"]) == \
        ("finalized", "extraction did not match", "no match")
    assert j["ext_note"] == "the Extractor stored match false: body HTTP 404"
    assert j["ext_record"]["match"] is False
    assert gw.bodies.read(j["body_name"]) is None


# ---- the retry rules -----------------------------------------------------------

def test_a_timeout_leaves_no_record_and_is_sent_again_with_the_same_url(active):
    gw = active
    job_id, _ = recorded(gw)
    gw.worker.tick()
    gw.worker.tick()
    first = last_extract(gw)
    gw.chain.script(first[0], TIMEOUT)
    gw.worker.tick()
    j = job(gw, job_id)
    assert (j["status"], j["stage"], j["ext_current_tx"]) == ("extracting", "serving body", None)
    assert gw.chain.extractions[EXTRACTOR] == {}
    # Still served, and the slot still held, between the attempts.
    assert gw.bodies.read(j["body_name"]) is not None
    assert gw.store.holder(EXTRACTOR)["owner"] == "job:%s" % (job_id,)

    gw.worker.tick()
    second = last_extract(gw)
    assert second[0] != first[0] and second[3] == first[3]
    gw.chain.script(second[0], AGREE)
    gw.chain.extract_outcome(second[0], "record")
    gw.worker.tick()
    j = job(gw, job_id)
    assert j["ext_status"] == "extracted" and j["ext_tx_ids"] == [first[0], second[0]]
    assert j["ext_attempts"] == 2


def test_three_attempts_without_execution_stop_the_extraction(active):
    gw = active
    job_id, _ = recorded(gw)
    gw.worker.tick()
    for _ in range(3):
        gw.worker.tick()
        gw.chain.script(last_extract(gw)[0], TIMEOUT)
        gw.worker.tick()
    j = job(gw, job_id)
    assert (j["status"], j["stage"], j["ext_status"]) == \
        ("finalized", "extraction stopped", "failed")
    assert j["ext_note"] == "3 extraction attempts, none executed"
    assert len(gw.chain.sends("extract")) == 3
    assert gw.bodies.read(j["body_name"]) is None


def test_executed_with_no_record_and_no_refusal_is_sent_again(active):
    gw = active
    job_id, _ = recorded(gw)
    gw.worker.tick()
    gw.worker.tick()
    gw.chain.script(last_extract(gw)[0], AGREE)
    gw.worker.tick()
    assert job(gw, job_id)["stage"] == "serving body"
    gw.worker.tick()
    assert len(gw.chain.sends("extract")) == 2


def test_an_earlier_record_for_the_same_verifier_record_is_not_this_calls(active):
    gw = active
    job_id, _ = recorded(gw)
    # Written before the snapshot: records_of lists it then, so it is not new.
    gw.chain.add_extraction(record_id="0")
    gw.worker.tick()
    gw.worker.tick()
    gw.chain.script(last_extract(gw)[0], AGREE)
    gw.worker.tick()
    assert job(gw, job_id)["stage"] == "serving body"
    assert job(gw, job_id)["ext_before"] == {"ids": ["0"], "refusal": ""}


def test_an_extraction_is_never_sent_again_while_undecided(active):
    gw = active
    job_id, _ = recorded(gw)
    gw.worker.tick()
    gw.worker.tick()
    gw.chain.script(last_extract(gw)[0], ACCEPTED)
    for _ in range(4):
        gw.worker.tick()
        gw.clock.advance(1800)
    assert len(gw.chain.sends("extract")) == 1
    gw.clock.advance(gw.settings.final_bound_s)
    gw.worker.tick()
    j = job(gw, job_id)
    assert j["ext_status"] == "failed" and "not FINALIZED" in j["ext_note"]
    assert gw.bodies.read(j["body_name"]) is None


# ---- one call at a time per contract ---------------------------------------------

def test_two_extractions_on_one_extractor_go_one_at_a_time(active):
    gw = active
    first, a = support.submit(gw, extract="auto")
    second, _ = support.submit(gw, extract="auto")
    scripted = set()
    for _ in range(14):
        gw.worker.tick()
        for tx_id, _, method, args, _ in gw.chain.sends():
            if tx_id in scripted:
                continue
            scripted.add(tx_id)
            gw.chain.script(tx_id, PENDING, AGREE)
            if method == "attest":
                # Both jobs carry the same message; claimed_records keeps
                # each record to one job.
                gw.chain.on_final[tx_id] = lambda: support.our_record(gw, a)
            else:
                gw.chain.extract_outcome(tx_id, "record")
        holder = gw.store.holder(EXTRACTOR)
        if holder:
            others = [j for j in (first, second) if "job:%s" % (j,) != holder["owner"]]
            for other in others:
                assert job(gw, other)["stage"] != "extracting"
    done = [job(gw, j) for j in (first, second)]
    assert [j["ext_status"] for j in done] == ["extracted", "extracted"]
    assert {j["ext_record_id"] for j in done} == {"0", "1"}
    assert {j["record_id"] for j in done} == {"0", "1"}


def test_the_verifier_and_the_extractor_have_separate_slots(active):
    gw = active
    first, _ = recorded(gw)
    second, _ = support.submit(gw, extract="auto")
    gw.worker.tick()
    # The first job serves its body while the second sends its attest.
    assert job(gw, first)["stage"] == "serving body"
    assert len(gw.chain.sends("attest")) == 2


# ---- skipped extractions ------------------------------------------------------

def test_a_body_over_the_cap_is_skipped_with_a_reason(active):
    gw = active
    raw = support.amazon_eml() + b"x" * headers.MAX_BODY
    job_id, _ = recorded(gw, raw=raw)
    j = job(gw, job_id)
    assert (j["status"], j["stage"], j["ext_status"]) == ("finalized", "recorded", "skipped")
    assert j["ext_note"] == "the body is over 262144 bytes"
    assert list(gw.bodies.staged.iterdir()) == [] and gw.chain.sends("extract") == []


def test_a_body_at_the_cap_is_kept():
    raw = support.amazon_eml()
    body = support.eml_body(raw)
    padded = raw + b"x" * (headers.MAX_BODY - len(body))
    chosen = headers.select(padded)
    served, why = headers.extraction_body(padded, chosen)
    # Padding breaks bh=, which is the next check; the cap let it through.
    assert served is None and why == "the body does not match the signature's bh="


def test_a_body_that_does_not_match_bh_is_not_served(active):
    gw = active
    raw = support.amazon_eml().replace(b"Arriving: Thursday", b"Arriving: Friday!")
    job_id, _ = recorded(gw, raw=raw)
    j = job(gw, job_id)
    assert (j["ext_status"], j["ext_note"]) == \
        ("skipped", "the body does not match the signature's bh=")
    assert list(gw.bodies.staged.iterdir()) == []


def test_an_invalid_verifier_record_skips_the_extraction(active):
    gw = active
    job_id, _ = recorded(gw, valid=False)
    j = job(gw, job_id)
    assert (j["status"], j["stage"], j["ext_status"]) == ("finalized", "recorded", "skipped")
    assert j["ext_note"] == "the Verifier record is not valid and aligned"
    assert not gw.bodies.is_staged(job_id) and gw.chain.sends("extract") == []


def test_a_moved_verifier_skips_the_extraction(active):
    gw = active
    job_id, _ = recorded(gw)
    gw.chain.resolves["verifier"] = support.VERIFIER_OLD
    gw.worker.tick()
    j = job(gw, job_id)
    assert (j["ext_status"], j["ext_note"]) == \
        ("skipped", "the Router now resolves another Verifier")
    assert not gw.bodies.is_staged(job_id) and gw.chain.sends("extract") == []


def test_a_refused_attestation_deletes_the_body(active):
    gw = active
    job_id, _ = support.submit(gw, extract="auto")
    assert gw.bodies.is_staged(job_id)
    gw.worker.tick()
    gw.chain.script(gw.chain.sends()[0][0], AGREE)
    gw.chain.refusal[VERIFIER] = "key pending"
    gw.worker.tick()
    assert job(gw, job_id)["status"] == "refused"
    assert not gw.bodies.is_staged(job_id)


def test_an_extractor_on_another_router_stops_before_serving(active):
    gw = active
    gw.chain.extractor_router[EXTRACTOR] = "0x" + "99" * 20
    job_id, _ = recorded(gw, extract="patterns")
    gw.worker.tick()
    j = job(gw, job_id)
    assert j["ext_status"] == "failed" and "resolves its Verifier through" in j["ext_note"]
    assert j["body_name"] is None and not gw.bodies.is_staged(job_id)


# ---- the store -------------------------------------------------------------------

def test_a_v0_database_gains_the_extraction_columns(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(str(path))
    db.executescript(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL, stage TEXT NOT NULL, "
        "sender_id INTEGER NOT NULL, headers_sha256 TEXT NOT NULL, bh TEXT NOT NULL, "
        "body_hash_ok INTEGER, blob_token TEXT, verifier TEXT, value_wei TEXT, "
        "records_before TEXT, attempts INTEGER NOT NULL DEFAULT 0, "
        "send_failures INTEGER NOT NULL DEFAULT 0, tx_ids TEXT NOT NULL DEFAULT '[]', "
        "current_tx TEXT, tx_status TEXT, record_id TEXT, valid_aligned INTEGER, "
        "refusal_reason TEXT, error TEXT, confirm_after REAL, created_at REAL NOT NULL, "
        "updated_at REAL NOT NULL, submitted_at REAL, decided_at REAL, finished_at REAL);"
        "CREATE INDEX jobs_open ON jobs (status) WHERE status IN ('pending', 'attesting');"
        "INSERT INTO jobs (id, status, stage, sender_id, headers_sha256, bh, created_at, "
        "updated_at) VALUES ('a', 'attesting', 'submitted', 1, 'h', 'b', 1, 1);")
    db.close()
    store = Store(path)
    old = store.job("a")
    assert old["extract_mode"] is None and old["ext_tx_ids"] == []
    assert [j["id"] for j in store.open_jobs()] == ["a"]


# ---- the API ---------------------------------------------------------------------

@pytest.fixture
def api(active):
    gw = active
    gw.client = TestClient(create_app(gw.settings, gw.store, gw.blobs, gw.contracts,
                                      SimpleNamespace(alive=lambda: True), clock=gw.clock,
                                      bodies=gw.bodies))
    return gw


def upload(api, raw=None, **form):
    return api.client.post("/attest", headers=AUTH, data=form,
                           files={"eml": ("m.eml", raw or support.amazon_eml(),
                                          "message/rfc822")})


def get_job(api, job_id):
    return api.client.get("/jobs/%s" % (job_id,), headers=AUTH).json()


def run_to_extracted(api, job_id):
    api.worker.tick()
    tx_id = api.chain.sends("attest")[-1][0]
    api.chain.script(tx_id, AGREE)
    chosen = SimpleNamespace(domain="amazon.com", selector="synthsel2026a",
                             bh=api.store.job(job_id)["bh"])
    api.chain.on_final[tx_id] = lambda: support.our_record(api, chosen)
    api.worker.tick()
    api.worker.tick()
    api.worker.tick()
    tx_id = last_extract(api)[0]
    api.chain.script(tx_id, AGREE)
    api.chain.extract_outcome(tx_id, "record")
    api.worker.tick()


def test_the_extract_field_is_checked_and_defaults_to_the_setting(api):
    assert upload(api, extract="all").status_code == 422
    body = upload(api, extract="LLM").json()
    assert body["extract"] == "llm"
    assert api.store.job(body["job_id"])["extract_mode"] == "llm"
    assert upload(api).json()["extract"] == "none"
    api.settings = support.settings(api.settings.data_dir, extract_default="auto")
    client = TestClient(create_app(api.settings, api.store, api.blobs, api.contracts,
                                   None, clock=api.clock, bodies=api.bodies))
    response = client.post("/attest", headers=AUTH,
                           files={"eml": ("m.eml", support.amazon_eml(), "message/rfc822")})
    job_id = response.json()["job_id"]
    assert response.json()["extract"] == "auto" and api.bodies.is_staged(job_id)


def test_the_job_json_carries_the_extraction(api):
    job_id = upload(api, extract="auto").json()["job_id"]
    body = get_job(api, job_id)
    assert body["extraction"]["status"] == "waiting for the attestation"
    assert body["extraction"]["requested"] == "auto" and body["extraction"]["lane"] is None

    run_to_extracted(api, job_id)
    body = get_job(api, job_id)
    assert (body["status"], body["stage"]) == ("finalized", "extracted")
    assert body["record_id"] == "0" and body["valid_and_aligned"] is True
    ext = body["extraction"]
    tx_id = last_extract(api)[0]
    assert ext == {
        "requested": "auto", "status": "extracted", "lane": "patterns",
        "extractor": EXTRACTOR, "attempts": 1, "consensus_tx": tx_id,
        "consensus_txs": [{"tx": tx_id, "explorer": "%s/tx/%s" % (support.EXPLORER, tx_id)}],
        "tx_status": "FINALIZED", "record_id": "0", "record": "/extractions/patterns/0",
        "match": True, "shipped": True, "eta_day": "jueves", "eta_date": "",
        "order_id_found": True, "method": "patterns",
        "patterns_sha256": support.EXTRACTED["patterns"]["patterns_sha256"],
        "reason": "extracted", "signed_at": "1790251922"}


def test_a_job_without_extraction_says_null(api):
    job_id = upload(api, extract="none").json()["job_id"]
    assert get_job(api, job_id)["extraction"] is None


def test_a_refused_extraction_in_the_job_json(api):
    api.chain.patterns.clear()
    job_id = upload(api, extract="patterns").json()["job_id"]
    api.worker.tick()
    api.chain.script(api.chain.sends("attest")[-1][0], AGREE)
    support.our_record(api, SimpleNamespace(domain="amazon.com", selector="synthsel2026a",
                                            bh=api.store.job(job_id)["bh"]))
    for _ in range(3):
        api.worker.tick()
    body = get_job(api, job_id)
    assert body["status"] == "finalized" and body["stage"] == "extraction refused"
    assert body["extraction"]["status"] == "refused"
    assert body["extraction"]["refusal_reason"] == "no patterns for domain"
    assert "reason" not in body["extraction"]


def test_a_record_lists_the_extractions_of_both_lanes(api):
    job_id = upload(api, extract="auto").json()["job_id"]
    run_to_extracted(api, job_id)
    api.chain.add_extraction(EXTRACTOR_LLM, record_id="0")
    # Same id on another Verifier, another record id, another requester: not listed.
    api.chain.add_extraction(EXTRACTOR, record_id="0", verifier=support.VERIFIER_OLD)
    api.chain.add_extraction(EXTRACTOR, record_id="7")
    api.chain.add_extraction(EXTRACTOR_LLM, record_id="0", requester="0x" + "ee" * 20)
    body = api.client.get("/records/0", headers=AUTH).json()
    assert body["verifier"] == VERIFIER and body["record"]["domain"] == "amazon.com"
    found = [(e["lane"], e["extractor"], e["id"]) for e in body["extractions"]]
    assert found == [("patterns", EXTRACTOR, "0"), ("llm", EXTRACTOR_LLM, "0")]
    assert body["extractions"][0]["record"]["reason"] == "extracted"
    assert all(final for a, m, _, final in api.chain.reads
               if m in ("records_of", "get_record"))


def test_one_extraction_record_is_read_at_latest_final(api):
    api.chain.add_extraction(EXTRACTOR_LLM, record_id="4")
    body = api.client.get("/extractions/llm/0", headers=AUTH).json()
    assert body == {"lane": "llm", "extractor": EXTRACTOR_LLM, "id": "0",
                    "read_at": "LATEST_FINAL",
                    "record": dict(support.EXTRACTED["llm"], id="0", record_id="4",
                                   requester=support.REQUESTER, fee_paid="0")}
    assert (EXTRACTOR_LLM, "get_record", ("0",), True) in api.chain.reads
    assert api.client.get("/extractions/llm/1", headers=AUTH).status_code == 404
    assert api.client.get("/extractions/regex/0", headers=AUTH).status_code == 400
    assert api.client.get("/extractions/llm/x", headers=AUTH).status_code == 400
    assert api.client.get("/extractions/llm/0").status_code == 401
    del api.chain.resolves["extractor_llm"]
    assert api.client.get("/extractions/llm/0", headers=AUTH).status_code == 404


def test_the_body_is_served_only_while_its_extraction_is_in_flight(api):
    job_id = upload(api, extract="auto").json()["job_id"]
    api.worker.tick()
    api.chain.script(api.chain.sends("attest")[-1][0], AGREE)
    support.our_record(api, SimpleNamespace(domain="amazon.com", selector="synthsel2026a",
                                            bh=api.store.job(job_id)["bh"]))
    api.worker.tick()
    assert list(api.bodies.served.iterdir()) == []
    api.worker.tick()
    name = api.store.job(job_id)["body_name"]
    served = api.client.get("/b/%s.bin" % (name,))
    assert served.status_code == 200 and served.content == support.eml_body()
    assert served.headers["content-type"] == "application/octet-stream"
    assert served.headers["cache-control"] == "no-store"
    assert api.client.get("/b/%s" % (name,)).status_code == 404
    assert api.client.get("/b/%s.bin" % ("0" * 32,)).status_code == 404
    assert api.client.get("/b/%s.bin" % (name.upper(),)).status_code == 404
    assert api.client.get("/b/..%2fgateway.sqlite3.bin").status_code == 404

    api.worker.tick()
    tx_id = last_extract(api)[0]
    api.chain.script(tx_id, ACCEPTED)
    api.worker.tick()
    assert api.client.get("/b/%s.bin" % (name,)).status_code == 200
    api.chain.script(tx_id, AGREE)
    api.chain.extract_outcome(tx_id, "record")
    api.worker.tick()
    assert api.client.get("/b/%s.bin" % (name,)).status_code == 404
    assert list(api.bodies.served.iterdir()) == [] and list(api.bodies.staged.iterdir()) == []


# ---- nothing personal ------------------------------------------------------------

def test_the_body_and_the_order_number_never_reach_the_log_or_the_database(api, caplog):
    caplog.set_level("DEBUG")
    job_id = upload(api, extract="auto").json()["job_id"]
    run_to_extracted(api, job_id)
    assert api.store.job(job_id)["ext_status"] == "extracted"
    api.client.get("/jobs/%s" % (job_id,), headers=AUTH)
    api.client.get("/records/0", headers=AUTH)
    logged = caplog.text
    dump = "\n".join(api.store._db.iterdump())
    name = api.store.job(job_id)["body_name"]
    for secret in (ORDER, "Arriving", "Thank you for your order", "_Part_000",
                   "customer@example.org", "/b/%s" % (name,)):
        assert secret not in logged, secret
        assert secret not in dump, secret
    assert ORDER not in api.client.get("/jobs/%s" % (job_id,), headers=AUTH).text
