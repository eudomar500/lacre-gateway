"""The sender-in-verification path: register_key, the quarantine, confirm_key.

The KeyCache stores a new key as pending and activates it only on a second
matching read at least 24 hours later (docs/interfaces.md section 9). The
Verifier refuses a pending key, so a job for a new sender waits, and says
until when.
"""

import pytest

import support
from support import AGREE, KEYCACHE, TIMEOUT, VERIFIER
from lacre_gateway.contracts import unix_time

KEY = ("amazon.com", "synthsel2026a")
FIRST_SEEN = "2026-09-21T12:00:00Z"


def pending():
    return dict(support.active_key(), state="pending", first_seen=FIRST_SEEN,
                activated_at="", refreshed_at="")


def finalize(gw, method, then=None):
    """Script the last call of method to FINALIZED, with then() at that point."""
    tx_id = gw.chain.sends(method)[-1][0]
    gw.chain.script(tx_id, AGREE)
    if then:
        gw.chain.on_final[tx_id] = then


def test_an_unknown_sender_is_registered_and_the_job_waits(gw):
    job_id, _ = support.submit(gw)
    gw.worker.tick()
    (tx_id, to, method, args, value), = gw.chain.sends()
    assert (to, method, args, value) == (KEYCACHE, "register_key", list(KEY), 0)
    j = gw.store.job(job_id)
    assert j["status"] == "pending" and j["stage"] == "sender in verification"
    assert j["confirm_after"] is None


def test_the_whole_path_to_an_attestation(gw):
    job_id, chosen = support.submit(gw)
    gw.clock.now = unix_time(FIRST_SEEN)
    gw.worker.tick()
    finalize(gw, "register_key", lambda: gw.chain.keys.__setitem__(KEY, pending()))
    gw.worker.tick()
    gw.worker.tick()
    j = gw.store.job(job_id)
    confirm_at = unix_time(FIRST_SEEN) + 24 * 3600 + gw.settings.confirm_margin_s
    assert j["stage"] == "sender in verification" and j["confirm_after"] == confirm_at
    assert gw.store.sender(*KEY)["state"] == "pending"

    # Not a second early: confirm_key before the quarantine reverts.
    gw.clock.now = confirm_at - 1
    gw.worker.tick()
    assert gw.chain.sends("confirm_key") == []

    gw.clock.now = confirm_at
    gw.worker.tick()
    (_, to, method, args, _), = gw.chain.sends("confirm_key")
    assert (to, args) == (KEYCACHE, list(KEY))
    assert gw.chain.sends("attest") == []

    finalize(gw, "confirm_key",
             lambda: gw.chain.keys.__setitem__(KEY, dict(pending(), state="active")))
    gw.worker.tick()
    # The key is active at LATEST_FINAL: the job resumes on the same pass.
    (_, to, method, _, _), = gw.chain.sends("attest")
    assert to == VERIFIER
    finalize(gw, "attest", lambda: support.our_record(gw, chosen))
    gw.worker.tick()
    assert gw.store.job(job_id)["status"] == "finalized"


def test_a_key_that_cannot_be_registered_fails_its_jobs(gw):
    job_id, _ = support.submit(gw)
    gw.worker.tick()
    gw.chain.failures[KEY] = "2026-09-21T12:00:00Z key revoked or absent"
    finalize(gw, "register_key")
    gw.worker.tick()
    j = gw.store.job(job_id)
    assert j["status"] == "failed"
    assert j["error"] == "sender key not usable: key revoked or absent"
    assert not gw.blobs.is_staged(job_id)


def test_a_register_that_did_not_execute_is_sent_again(gw):
    support.submit(gw)
    gw.worker.tick()
    gw.chain.script(gw.chain.sends()[0][0], TIMEOUT)
    gw.worker.tick()
    gw.worker.tick()
    assert len(gw.chain.sends("register_key")) == 2


def test_a_resolver_outage_at_confirm_leaves_the_key_pending_and_retries(gw):
    job_id, _ = support.submit(gw)
    gw.chain.keys[KEY] = pending()
    gw.clock.now = unix_time(FIRST_SEEN) + 25 * 3600
    gw.worker.tick()
    assert len(gw.chain.sends("confirm_key")) == 1
    gw.chain.failures[KEY] = "2026-09-22T13:00:00Z resolver unavailable: dns.google"
    finalize(gw, "confirm_key")
    gw.worker.tick()
    assert gw.store.job(job_id)["status"] == "pending"
    assert gw.store.sender(*KEY)["reason"] == "resolver unavailable: dns.google"
    gw.clock.advance(gw.settings.confirm_retry_s - 1)
    gw.worker.tick()
    assert len(gw.chain.sends("confirm_key")) == 1
    gw.clock.advance(1)
    gw.worker.tick()
    assert len(gw.chain.sends("confirm_key")) == 2


def test_a_key_that_changed_during_quarantine_fails_its_jobs(gw):
    job_id, _ = support.submit(gw)
    gw.chain.keys[KEY] = pending()
    gw.clock.now = unix_time(FIRST_SEEN) + 25 * 3600
    gw.worker.tick()
    gw.chain.failures[KEY] = "2026-09-22T13:00:00Z key changed during quarantine"

    def dropped():
        del gw.chain.keys[KEY]
    finalize(gw, "confirm_key", dropped)
    gw.worker.tick()
    j = gw.store.job(job_id)
    assert j["status"] == "failed" and "key changed during quarantine" in j["error"]


def test_jobs_for_the_same_sender_share_one_registration(gw):
    first, _ = support.submit(gw)
    second, _ = support.submit(gw)
    gw.worker.tick()
    gw.worker.tick()
    assert len(gw.chain.sends("register_key")) == 1
    for job_id in (first, second):
        assert gw.store.job(job_id)["stage"] == "sender in verification"


@pytest.mark.parametrize("state", ["rotated", "retired"])
def test_a_key_that_turns_rotated_or_retired_refuses_its_jobs(gw, state):
    job_id, _ = support.submit(gw)
    gw.chain.keys[KEY] = dict(support.active_key(), state=state)
    gw.worker.tick()
    j = gw.store.job(job_id)
    assert (j["status"], j["refusal_reason"]) == ("refused", "key %s" % (state,))
    assert gw.chain.sends() == []
