"""The job state machine and the confirmation protocol decisions.

Stored states are the recorded Bradbury answers in tests/fixtures (see
support.py). A job only finishes on a record read back at LATEST_FINAL; it is
sent again only after a finalization without execution; never while its call
is undecided, appealed or CANCELED.
"""

import pytest

import support
from support import (ACCEPTED, AGREE, APPEALED, CANCELED, PENDING, TIMEOUT, UNDERPAID,
                     UNDETERMINED, VERIFIER)
from lacre_gateway import worker as W
from lacre_gateway.vendor import txstate


@pytest.fixture
def active(gw):
    gw.chain.keys[("amazon.com", "synthsel2026a")] = support.active_key()
    return gw


def job(gw, job_id):
    return gw.store.job(job_id)


# ---- the recorded states, as the gateway reads them ------------------------

def test_the_recorded_states_mean_what_the_public_repo_says():
    assert not txstate.executed(TIMEOUT) and TIMEOUT["status"] == "FINALIZED"
    assert txstate.executed(AGREE)
    assert txstate.executed(UNDERPAID)


@pytest.mark.parametrize("state", [PENDING, ACCEPTED, UNDETERMINED, CANCELED, APPEALED],
                         ids=["pending", "accepted", "undetermined", "canceled", "appeal"])
def test_nothing_is_concluded_before_finalized(state):
    assert W.decide(state, None, support.REQUESTER, bound_reached=False)[0] == W.WAIT


@pytest.mark.parametrize("state", [PENDING, ACCEPTED, UNDETERMINED, CANCELED, APPEALED],
                         ids=["pending", "accepted", "undetermined", "canceled", "appeal"])
def test_at_the_bound_anything_not_finalized_stops(state):
    assert W.decide(state, None, support.REQUESTER, bound_reached=True)[0] == W.STOP


def test_finalized_without_executing_is_sent_again():
    verdict, message = W.decide(TIMEOUT, None, support.REQUESTER, False)
    assert verdict == W.RESEND and "TIMEOUT" in message


def test_executed_with_our_record_is_recorded():
    found = {"records": [("4", dict(support.RECORD, requester=support.REQUESTER))],
             "reason": None}
    assert W.decide(AGREE, found, support.REQUESTER, False)[0] == W.RECORDED


def test_executed_with_a_reason_is_refused():
    found = {"records": [], "reason": "fee not paid"}
    assert W.decide(UNDERPAID, found, support.REQUESTER, False)[0] == W.REFUSED


def test_executed_without_a_record_or_a_reason_is_sent_again():
    found = {"records": [], "reason": None}
    assert W.decide(AGREE, found, support.REQUESTER, False)[0] == W.RESEND


# ---- the state machine -----------------------------------------------------

def test_a_job_goes_pending_attesting_finalized(active):
    gw = active
    gw.chain.fee = 7
    job_id, chosen = support.submit(gw)
    assert job(gw, job_id)["status"] == "pending"

    gw.worker.tick()
    j = job(gw, job_id)
    assert j["status"] == "attesting" and j["attempts"] == 1
    (tx_id, to, method, args, value), = gw.chain.sends()
    assert (to, method, value) == (VERIFIER, "attest", 7)
    url, domain, selector = args
    assert (domain, selector) == ("amazon.com", "synthsel2026a")
    token = j["blob_token"]
    assert url == gw.blobs.url(token)
    assert gw.blobs.read(token) == chosen.blob
    assert j["verifier"] == VERIFIER and j["records_before"] == []

    gw.chain.script(tx_id, ACCEPTED, AGREE)
    gw.chain.on_final[tx_id] = lambda: support.our_record(gw, chosen, fee_paid="7")
    gw.worker.tick()
    j = job(gw, job_id)
    assert j["status"] == "attesting" and j["tx_status"] == "ACCEPTED"
    assert gw.blobs.read(token) == chosen.blob

    gw.worker.tick()
    j = job(gw, job_id)
    assert j["status"] == "finalized"
    assert j["record_id"] == "0" and j["verifier"] == VERIFIER
    assert j["tx_ids"] == [tx_id]
    assert gw.blobs.read(token) is None
    assert gw.store.inflight() == []


def test_the_record_is_found_through_records_of_at_latest_final(active):
    gw = active
    for _ in range(3):
        gw.chain.add_record(requester="0x" + "ee" * 20)
    job_id, chosen = support.submit(gw)
    gw.worker.tick()
    tx_id = gw.chain.sends()[0][0]
    gw.chain.script(tx_id, AGREE)
    support.our_record(gw, chosen)
    gw.chain.reads.clear()
    gw.worker.tick()
    assert job(gw, job_id)["record_id"] == "3"
    reads = [(m, a) for _, m, a, _ in gw.chain.reads]
    assert ("count", ()) not in reads
    assert [a for m, a in reads if m == "get"] == [("3",)]
    assert all(final for _, m, _, final in gw.chain.reads if m in ("records_of", "get"))


def test_a_record_that_existed_before_the_first_attempt_is_not_ours(active):
    gw = active
    job_id, chosen = support.submit(gw)
    support.our_record(gw, chosen)
    gw.worker.tick()
    tx_id = gw.chain.sends()[0][0]
    gw.chain.script(tx_id, AGREE)
    gw.worker.tick()
    # No new record and no refusal: executed but unreadable, so send again.
    j = job(gw, job_id)
    assert j["status"] == "attesting" and j["stage"] == "sending again"


def test_a_record_with_another_bh_is_not_ours(active):
    gw = active
    job_id, chosen = support.submit(gw)
    gw.worker.tick()
    tx_id = gw.chain.sends()[0][0]
    gw.chain.script(tx_id, AGREE)
    support.our_record(gw, chosen, bh="another")
    gw.chain.refusal[VERIFIER] = "fee not paid"
    gw.worker.tick()
    assert job(gw, job_id)["status"] == "refused"


def test_timeout_then_agree_sends_twice_with_the_same_url(active):
    gw = active
    job_id, chosen = support.submit(gw)
    gw.worker.tick()
    first = gw.chain.sends()[0]
    gw.chain.script(first[0], TIMEOUT)
    gw.worker.tick()
    assert job(gw, job_id)["stage"] == "sending again"
    gw.worker.tick()
    second = gw.chain.sends()[1]
    assert second[3][0] == first[3][0]
    gw.chain.script(second[0], AGREE)
    support.our_record(gw, chosen)
    gw.worker.tick()
    j = job(gw, job_id)
    assert j["status"] == "finalized" and j["tx_ids"] == [first[0], second[0]]
    assert j["attempts"] == 2


def test_attempts_run_out_after_finalizations_without_execution(active):
    gw = active
    job_id, _ = support.submit(gw)
    for attempt in range(3):
        gw.worker.tick()
        gw.chain.script(gw.chain.sends()[-1][0], TIMEOUT)
        gw.worker.tick()
    j = job(gw, job_id)
    assert j["status"] == "failed" and "3 attempts, none executed" in j["error"]
    assert len(gw.chain.sends()) == 3
    assert gw.blobs.read(j["blob_token"]) is None


def test_a_refusal_is_final_and_names_the_reason(active):
    gw = active
    job_id, _ = support.submit(gw)
    gw.worker.tick()
    gw.chain.script(gw.chain.sends()[0][0], UNDERPAID)
    gw.chain.refusal[VERIFIER] = "fee not paid"
    gw.worker.tick()
    gw.worker.tick()
    j = job(gw, job_id)
    assert j["status"] == "refused" and j["refusal_reason"] == "fee not paid"
    assert len(gw.chain.sends()) == 1
    assert gw.blobs.read(j["blob_token"]) is None


@pytest.mark.parametrize("state", [CANCELED, APPEALED, UNDETERMINED, ACCEPTED],
                         ids=["canceled", "appeal", "undetermined", "accepted"])
def test_undecided_appealed_or_canceled_is_never_sent_again(active, state):
    gw = active
    job_id, _ = support.submit(gw)
    gw.worker.tick()
    gw.chain.script(gw.chain.sends()[0][0], state)
    for _ in range(5):
        gw.worker.tick()
        gw.clock.advance(1800)
    assert len(gw.chain.sends()) == 1
    assert job(gw, job_id)["status"] == "attesting"
    gw.clock.advance(gw.settings.final_bound_s)
    gw.worker.tick()
    j = job(gw, job_id)
    assert j["status"] == "failed" and len(gw.chain.sends()) == 1
    assert gw.blobs.read(j["blob_token"]) is None


def test_a_canceled_call_that_is_later_finalized_is_recorded(active):
    gw = active
    job_id, chosen = support.submit(gw)
    gw.worker.tick()
    tx_id = gw.chain.sends()[0][0]
    gw.chain.script(tx_id, CANCELED, CANCELED, AGREE)
    gw.chain.on_final[tx_id] = lambda: support.our_record(gw, chosen)
    for _ in range(3):
        gw.worker.tick()
    assert job(gw, job_id)["status"] == "finalized"
    assert len(gw.chain.sends()) == 1


def test_a_rotated_or_retired_key_is_refused_without_sending(gw):
    job_id, _ = support.submit(gw)
    gw.chain.keys[("amazon.com", "synthsel2026a")] = dict(support.active_key(), state="rotated")
    gw.worker.tick()
    j = job(gw, job_id)
    assert j["status"] == "refused" and j["refusal_reason"] == "key rotated"
    assert gw.chain.sends("attest") == [] and j["blob_token"] is None
    assert not gw.blobs.is_staged(job_id)


def test_nothing_sent_is_tried_again_then_fails(active):
    gw = active
    job_id, _ = support.submit(gw)
    gw.chain.fail_next_send = [support.NothingSent("the gas estimate failed")] * 3
    gw.worker.tick()
    assert job(gw, job_id)["status"] == "pending"
    assert gw.store.inflight() == []
    gw.worker.tick()
    gw.worker.tick()
    j = job(gw, job_id)
    assert j["status"] == "failed" and j["send_failures"] == 3
    assert gw.chain.sends() == []


def test_a_send_with_an_unknown_outcome_stops_and_is_not_repeated(active):
    gw = active
    job_id, _ = support.submit(gw)
    gw.chain.fail_next_send = [support.Stop("broadcast outcome unknown")]
    gw.worker.tick()
    gw.worker.tick()
    j = job(gw, job_id)
    assert j["status"] == "failed" and "outcome unknown" in j["error"]
    assert gw.blobs.read(j["blob_token"]) is None


def test_the_fee_is_read_just_before_each_send(active):
    gw = active
    job_id, chosen = support.submit(gw)
    gw.chain.fee = 5
    gw.worker.tick()
    gw.chain.script(gw.chain.sends()[0][0], TIMEOUT)
    gw.chain.fee = 9
    gw.worker.tick()
    gw.worker.tick()
    assert [s[4] for s in gw.chain.sends()] == [5, 9]


def test_without_a_signing_key_nothing_is_sent(active):
    gw = active
    gw.chain.can_sign = False
    job_id, _ = support.submit(gw)
    gw.worker.tick()
    assert gw.chain.sends() == [] and job(gw, job_id)["status"] == "pending"


def test_a_chain_outage_leaves_the_job_where_it_was(active):
    gw = active
    job_id, _ = support.submit(gw)
    gw.worker.tick()
    gw.chain.up = False
    gw.worker.tick()
    gw.chain.up = True
    assert job(gw, job_id)["status"] == "attesting"
    assert len(gw.chain.sends()) == 1


def test_the_worker_beats(gw):
    gw.worker.tick()
    assert gw.store.heartbeat() == gw.clock()


def test_nothing_personal_reaches_the_database_or_the_log(active, caplog, tmp_path):
    gw = active
    caplog.set_level("DEBUG")
    job_id, chosen = support.submit(gw)
    gw.worker.tick()
    gw.chain.script(gw.chain.sends()[0][0], AGREE)
    support.our_record(gw, chosen)
    gw.worker.tick()
    assert job(gw, job_id)["status"] == "finalized"
    dump = "\n".join(gw.store._db.iterdump()).lower()
    logged = caplog.text.lower()
    for secret in ("customer@example.org", "auto-confirm", "no-reply", "your amazon.com order",
                   "thank you", "arriving", "message-id", "sun, 20 sep"):
        assert secret not in dump, secret
        assert secret not in logged, secret
    assert "amazon.com" not in logged
