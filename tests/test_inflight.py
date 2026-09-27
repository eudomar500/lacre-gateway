"""One call in flight per contract, across all jobs and sender keys.

docs/interfaces.md section 5, rule 13: ConsensusMain queues a second
transaction to a contract behind an undecided one, and a client must not
send it. FakeChain fails any test in which the gateway does.
"""

import pytest

import support
from support import ACCEPTED, AGREE, APPEALED, KEYCACHE, PENDING, VERIFIER


@pytest.fixture
def active(gw):
    gw.chain.keys[("amazon.com", "synthsel2026a")] = support.active_key()
    return gw


def test_the_slot_is_one_per_contract(gw):
    assert gw.store.acquire(VERIFIER, "job:a")
    assert gw.store.acquire(VERIFIER.upper().replace("0X", "0x"), "job:a")
    assert not gw.store.acquire(VERIFIER, "job:b")
    assert gw.store.acquire(KEYCACHE, "job:b")
    gw.store.release(VERIFIER, "job:b")
    assert not gw.store.acquire(VERIFIER, "job:b")
    gw.store.release(VERIFIER, "job:a")
    assert gw.store.acquire(VERIFIER, "job:b")


def test_a_second_job_waits_until_the_first_call_is_decided(active):
    gw = active
    first, a = support.submit(gw)
    second, b = support.submit(gw)
    gw.worker.tick()
    assert len(gw.chain.sends()) == 1
    assert gw.store.job(second)["stage"] == "waiting for the Verifier"
    tx_a = gw.chain.sends()[0][0]
    gw.chain.script(tx_a, PENDING, ACCEPTED, AGREE)

    gw.worker.tick()
    assert len(gw.chain.sends()) == 1

    gw.worker.tick()
    # ACCEPTED is decided: the slot is free and the second call goes out on
    # the same pass, while the first still waits for FINALIZED.
    assert len(gw.chain.sends()) == 2
    assert gw.store.job(first)["status"] == "attesting"
    assert gw.store.holder(VERIFIER)["owner"] == "job:%s" % (second,)


def test_an_appeal_keeps_the_slot(active):
    gw = active
    first, _ = support.submit(gw)
    support.submit(gw)
    gw.worker.tick()
    gw.chain.script(gw.chain.sends()[0][0], APPEALED)
    for _ in range(4):
        gw.worker.tick()
    assert len(gw.chain.sends()) == 1
    assert gw.store.holder(VERIFIER)["owner"] == "job:%s" % (first,)


def test_many_jobs_go_out_one_at_a_time(active):
    gw = active
    jobs = [support.submit(gw) for _ in range(5)]
    scripted = set()
    for _ in range(12):
        gw.worker.tick()
        for tx_id, *_ in gw.chain.sends():
            if tx_id not in scripted:
                scripted.add(tx_id)
                gw.chain.script(tx_id, PENDING, AGREE)
                gw.chain.on_final[tx_id] = lambda c=jobs[0][1]: support.our_record(gw, c)
    # FakeChain asserted on every send that nothing else was undecided.
    finished = [gw.store.job(j) for j, _ in jobs]
    assert [j["status"] for j in finished] == ["finalized"] * 5
    assert len(gw.chain.sends("attest")) == 5
    assert len({j["record_id"] for j in finished}) == 5


def test_the_keycache_and_the_verifier_have_separate_slots(gw):
    gw.chain.keys[("amazon.com", "synthsel2026a")] = support.active_key()
    ready, _ = support.submit(gw)
    raw = support.amazon_eml().replace(b"s=synthsel2026a", b"s=synthsel2026z")
    waiting, _ = support.submit(gw, raw)
    gw.worker.tick()
    assert [s[2] for s in gw.chain.sends()] == ["register_key", "attest"]


def test_two_unknown_senders_register_one_at_a_time(gw):
    one = support.amazon_eml().replace(b"s=synthsel2026a", b"s=synthsel2026y")
    two = support.amazon_eml().replace(b"s=synthsel2026a", b"s=synthsel2026z")
    support.submit(gw, one)
    support.submit(gw, two)
    gw.worker.tick()
    assert len(gw.chain.sends("register_key")) == 1
    gw.worker.tick()
    assert len(gw.chain.sends("register_key")) == 1
    gw.chain.script(gw.chain.sends()[0][0], AGREE)
    gw.worker.tick()
    assert len(gw.chain.sends("register_key")) == 2
    assert gw.chain.sends()[1][1] == KEYCACHE
