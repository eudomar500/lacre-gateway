"""The background worker: sender keys, sends, and the confirmation protocol.

One pass (tick) does, in order:

1. Sender keys. For every sender an open job waits on: an unknown key gets a
   KeyCache.register_key; a pending key whose quarantine has passed gets a
   confirm_key; a KeyCache call in flight is followed to FINALIZED and the
   key state read back at LATEST_FINAL decides what happened, since the
   return value of a write cannot be read (docs/interfaces.md 5.15).
2. Jobs. A pending job whose key is active is sent; a job with a call in
   flight is followed to FINALIZED and judged with attest.judge, the same
   decision tools/attest.py makes: recorded, refused, send again, or stop.

The rules this enforces, from docs/interfaces.md section 5:

- One call in flight per contract, across all jobs and sender keys (rule
  13). The inflight table has the contract as its primary key; a job or a
  sender takes the slot before sending and gives it back when the stored
  status is decided, or when nothing was sent.
- A job is finished only on a record read at LATEST_FINAL from records_of
  and get (rules 1, 10). A status, a return value or an eq output never
  finishes one.
- A call is sent again only after it FINALIZED without executing (or
  executed and left no readable record and no refusal), and never while it
  is undecided, appealed or CANCELED (rule 11). A stored CANCELED is not
  final (rule 14), so it is waited on like an undecided status until the
  bound, and then the job stops without sending again.
- attest carries exactly fee(), read just before the send (rule 9).

Nothing personal is logged: job ids, tx ids and statuses only.
"""

import logging
import threading
import time

from .chainio import ChainUnavailable, NothingSent, Stop
from .contracts import confirm_after, sender_state
from .vendor import attest, txstate

log = logging.getLogger("lacre_gateway.worker")

RECORDED, REFUSED, RESEND, STOP = attest.RECORDED, attest.REFUSED, attest.RESEND, attest.STOP
WAIT = "wait"

READY = "ready"
IN_VERIFICATION = "sender in verification"


def decide(state, found, requester, bound_reached):
    """(verdict, message) for one call's stored state.

    Before FINALIZED the answer is WAIT, whatever the status: undecided,
    ACCEPTED, appealed or CANCELED, nothing is sent and nothing is
    concluded. From FINALIZED on, or once the bound is reached, it is
    attest.judge's verdict with --until finalized, which STOPs on anything
    that is not FINALIZED.
    """
    if state["status"] != "FINALIZED" and not bound_reached:
        return WAIT, "stored status %s" % (state["status"],)
    return attest.judge(state, "finalized", found, requester)


def settled(state):
    """Whether a call no longer blocks the next one to the same contract.

    ConsensusMain activates the next transaction to a contract once the
    current one is decided (rule 13). An appeal makes it undecided again.
    """
    return state["status"] in txstate.DECIDED and state["status"] not in txstate.APPEAL


def matches(record, call, bh):
    """A record this job's call wrote: attest.ours, and the job's bh.

    attest.ours checks requester, domain, selector, source and fee_paid; bh
    is added because one gateway key attests for everyone, so two jobs for
    the same sender differ only in what was signed.
    """
    return attest.ours(record, call) and record.get("bh") == bh


class Worker:
    def __init__(self, settings, store, blobs, contracts, clock=time.time):
        self.settings = settings
        self.store = store
        self.blobs = blobs
        self.contracts = contracts
        self.chain = contracts.chain
        self.clock = clock
        self.thread = None
        self._stop = threading.Event()

    # ---- the loop ----------------------------------------------------------

    def start(self):
        self.thread = threading.Thread(target=self.run, name="lacre-worker", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        if self.thread:
            self.thread.join(timeout=10)

    def alive(self):
        return self.thread is not None and self.thread.is_alive()

    def run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as error:
                log.error("worker pass failed: %s", type(error).__name__)
            self._stop.wait(self.settings.poll_s)

    def tick(self):
        self.store.beat()
        for sender in self.store.waiting_senders():
            self._guard("sender %d" % (sender["id"],), self.sender_step, sender)
        for job in self.store.open_jobs():
            self._guard("job %s" % (job["id"],), self.job_step, job)
        self.store.beat()

    def _guard(self, what, step, row):
        try:
            step(row)
        except ChainUnavailable as error:
            log.warning("%s: chain unavailable (%s), retried next pass", what, error)
        except Exception as error:
            log.error("%s: %s, retried next pass", what, type(error).__name__)

    # ---- sender keys ---------------------------------------------------------

    def _owner(self, sender):
        return "sender:%d" % (sender["id"],)

    def sender_step(self, sender):
        now = self.clock()
        if sender["op_tx"]:
            return self._follow_key_call(sender, now)
        status = self.contracts.key_status(sender["domain"], sender["selector"])
        state = sender_state(status)
        values = {"state": state}
        if state == "pending":
            values["confirm_after"] = confirm_after(status, self.settings.key_quarantine_s)
        self.store.update_sender(sender["id"], **values)
        if now < (sender["next_at"] or 0):
            return
        if state == "unknown":
            self._send_key_call(sender, "register_key", now)
        elif state == "pending" and now >= values["confirm_after"] + self.settings.confirm_margin_s:
            self._send_key_call(sender, "confirm_key", now)

    def _send_key_call(self, sender, method, now):
        if not self.chain.can_sign:
            return
        keycache = self.contracts.keycache()
        owner = self._owner(sender)
        if not self.store.acquire(keycache, owner):
            return
        try:
            tx_id = self.chain.send(keycache, method, [sender["domain"], sender["selector"]], 0)
        except NothingSent as error:
            self.store.release(keycache, owner)
            log.warning("sender %d: %s not sent (%s)", sender["id"], method, error)
            self.store.update_sender(sender["id"], next_at=now + self.settings.poll_s)
            return
        except Stop as error:
            self.store.release(keycache, owner)
            log.error("sender %d: %s stopped (%s)", sender["id"], method, error)
            self._fail_sender_jobs(sender, "sender key call stopped: %s" % (error,))
            self.store.update_sender(sender["id"], next_at=now + self.settings.confirm_retry_s)
            return
        self.store.set_inflight_tx(keycache, owner, tx_id)
        self.store.update_sender(sender["id"], op=method, op_tx=tx_id, op_sent_at=now,
                                 op_attempts=(sender["op_attempts"] or 0) + 1)
        log.info("sender %d: %s sent, tx %s", sender["id"], method, tx_id)

    def _follow_key_call(self, sender, now):
        state = self.chain.stored(sender["op_tx"])
        owner = self._owner(sender)
        if settled(state):
            self.store.release_owner(owner)
        bound = now - (sender["op_sent_at"] or now) >= self.settings.final_bound_s
        if state["status"] != "FINALIZED" and not bound:
            return
        cleared = {"op": None, "op_tx": None, "op_sent_at": None}
        self.store.release_owner(owner)
        if state["status"] != "FINALIZED":
            log.error("sender %d: %s not FINALIZED at the bound (%s)", sender["id"],
                      sender["op"], state["status"])
            self.store.update_sender(sender["id"], op_attempts=0, **cleared)
            self._fail_sender_jobs(sender, "the sender key call was not FINALIZED at the bound")
            return
        # Every outcome of a KeyCache write is a view: read the key back.
        status = self.contracts.key_status(sender["domain"], sender["selector"])
        key = sender_state(status)
        log.info("sender %d: %s %s, key %s", sender["id"], sender["op"],
                 "executed" if txstate.executed(state) else "not executed", key)
        if key == "active":
            self.store.update_sender(sender["id"], state=key, op_attempts=0, reason=None,
                                     **cleared)
        elif key == "pending":
            values = dict(cleared, state=key,
                          confirm_after=confirm_after(status, self.settings.key_quarantine_s))
            if sender["op"] == "confirm_key":
                # A resolver was down, or the quarantine had not passed on
                # the runner's clock: the key stays pending, try later.
                values["next_at"] = now + self.settings.confirm_retry_s
                values["reason"] = self.contracts.last_failure(sender["domain"],
                                                               sender["selector"]) or None
            self.store.update_sender(sender["id"], op_attempts=0, **values)
        elif key in ("rotated", "retired"):
            self.store.update_sender(sender["id"], state=key, op_attempts=0, **cleared)
        elif not txstate.executed(state) and (sender["op_attempts"] or 0) < self.settings.max_attempts:
            # FINALIZED without executing: nothing was written, the call can
            # be sent again (rule 11). The next pass does it.
            self.store.update_sender(sender["id"], state=key, **cleared)
        else:
            reason = self.contracts.last_failure(sender["domain"], sender["selector"])
            reason = reason or "the sender key could not be registered"
            self.store.update_sender(sender["id"], state=key, op_attempts=0, reason=reason,
                                     next_at=now + self.settings.confirm_retry_s, **cleared)
            self._fail_sender_jobs(sender, "sender key not usable: %s" % (reason,))

    def _fail_sender_jobs(self, sender, message):
        for job in self.store.open_jobs():
            if job["sender_id"] == sender["id"] and job["status"] == "pending":
                self._finish(job, "failed", "stopped", error=message)

    # ---- jobs ----------------------------------------------------------------

    def job_step(self, job):
        if job["current_tx"]:
            return self._follow(job)
        sender = self.store.sender_by_id(job["sender_id"])
        if job["status"] == "pending":
            status = self.contracts.key_status(sender["domain"], sender["selector"])
            key = sender_state(status)
            if key in ("rotated", "retired"):
                # The Verifier refuses these ("key rotated", "key retired"):
                # nothing is sent and the headers are never published.
                return self._finish(job, "refused", "refused before sending",
                                    refusal_reason="key %s" % (key,))
            if key != "active":
                when = (confirm_after(status, self.settings.key_quarantine_s)
                        + self.settings.confirm_margin_s) if key == "pending" else None
                self.store.update_job(job["id"], stage=IN_VERIFICATION, confirm_after=when)
                return
        self._send(job, sender)

    def _send(self, job, sender):
        if not self.chain.can_sign:
            return
        now = self.clock()
        verifier = job["verifier"] or self.contracts.verifier()
        owner = "job:%s" % (job["id"],)
        if not self.store.acquire(verifier, owner):
            self.store.update_job(job["id"], stage="waiting for the Verifier")
            return
        try:
            requester = self.chain.requester
            fee = self.contracts.fee(verifier)
            values = {}
            if job["records_before"] is None:
                values["records_before"] = self.contracts.records_of(verifier, requester)
            token = job["blob_token"]
            if token is None:
                if not self.blobs.is_staged(job["id"]):
                    self.store.release(verifier, owner)
                    return self._finish(job, "failed", "stopped", error="the headers are gone")
                token, _ = self.blobs.publish(job["id"])
                values["blob_token"] = token
                # Stored before the send: the file has to be deleted
                # whatever happens next.
                self.store.update_job(job["id"], blob_token=token, verifier=verifier)
            url = self.blobs.url(token)
            if len(url) > attest.MAX_URL:
                self.store.release(verifier, owner)
                return self._finish(job, "failed", "stopped", error="the blob URL is too long")
        except Exception:
            self.store.release(verifier, owner)
            raise
        try:
            tx_id = self.chain.send(verifier, "attest", [url, sender["domain"], sender["selector"]],
                                    fee)
        except NothingSent as error:
            self.store.release(verifier, owner)
            failures = job["send_failures"] + 1
            log.warning("job %s: not sent (%s), failure %d", job["id"], error, failures)
            if failures >= self.settings.max_send_failures:
                return self._finish(job, "failed", "stopped", send_failures=failures,
                                    error="not sent: %s" % (error,), **values)
            self.store.update_job(job["id"], send_failures=failures, verifier=verifier, **values)
            return
        except Stop as error:
            self.store.release(verifier, owner)
            log.error("job %s: send stopped (%s)", job["id"], error)
            return self._finish(job, "failed", "stopped", verifier=verifier,
                                error="stopped: %s" % (error,), **values)
        self.store.set_inflight_tx(verifier, owner, tx_id)
        self.store.update_job(
            job["id"], status="attesting", stage="submitted", verifier=verifier,
            value_wei=str(fee), attempts=job["attempts"] + 1,
            tx_ids=job["tx_ids"] + [tx_id], current_tx=tx_id, tx_status="PENDING",
            submitted_at=now, decided_at=None, **values)
        log.info("job %s: attempt %d sent, tx %s", job["id"], job["attempts"] + 1, tx_id)

    def _follow(self, job):
        now = self.clock()
        state = self.chain.stored(job["current_tx"])
        owner = "job:%s" % (job["id"],)
        values = {"tx_status": state["status"]}
        if settled(state):
            self.store.release(job["verifier"], owner)
            if not job["decided_at"]:
                values["decided_at"] = now
        bound = now - (job["submitted_at"] or now) >= self.settings.final_bound_s
        requester = self.chain.requester
        found = None
        if state["status"] == "FINALIZED" and txstate.executed(state):
            found = self.outcome(job, requester)
        verdict, message = decide(state, found, requester, bound)
        if verdict == WAIT:
            values["stage"] = "waiting for FINALIZED"
            self.store.update_job(job["id"], **values)
            return
        self.store.release(job["verifier"], owner)
        log.info("job %s: %s (tx %s)", job["id"], verdict, job["current_tx"])
        if verdict == RECORDED:
            record_id, record = found["records"][0]
            # A record means the check ran, not that it passed (rule 2).
            verdict_ok = bool(record.get("valid")) and bool(record.get("aligned"))
            return self._finish(job, "finalized", "recorded", record_id=record_id,
                                valid_aligned=int(verdict_ok), **values)
        if verdict == REFUSED:
            return self._finish(job, "refused", "refused by the Verifier",
                                refusal_reason=found["reason"], **values)
        if verdict == RESEND and job["attempts"] < self.settings.max_attempts:
            self.store.update_job(job["id"], stage="sending again", current_tx=None, **values)
            return
        if verdict == RESEND:
            message = "%d attempts, none executed" % (job["attempts"],)
        return self._finish(job, "failed", "stopped", error=message, **values)

    def outcome(self, job, requester):
        """attest.outcome for Verifier v1.2: records_of instead of a scan.

        {"records": [(id, record)], "reason": str or None}. A record is this
        job's when it is new since the first attempt, matches the call, and no
        other job has claimed it. An executed call that wrote no record was
        refused (every executed attest either records or refuses), and v1.2
        keeps the reason in last_refusal(requester).
        """
        verifier = job["verifier"]
        before = set(job["records_before"] or [])
        claimed = self.store.claimed_records(verifier, job["id"])
        sender = self.store.sender_by_id(job["sender_id"])
        call = {"sender": requester, "domain": sender["domain"], "selector": sender["selector"],
                "method": "attest", "value": int(job["value_wei"] or 0)}
        records = []
        for record_id in self.contracts.records_of(verifier, requester):
            if record_id in before or record_id in claimed:
                continue
            record = self.contracts.record(verifier, record_id)
            if matches(record, call, job["bh"]):
                records.append((record_id, record))
        if records:
            return {"records": records, "reason": None}
        reason = self.contracts.last_refusal(verifier, requester)
        return {"records": [], "reason": reason or None}

    def _finish(self, job, status, stage, **values):
        self.blobs.delete(job["id"], values.get("blob_token", job["blob_token"]))
        self.store.release_owner("job:%s" % (job["id"],))
        self.store.update_job(job["id"], status=status, stage=stage, current_tx=None, **values)
        log.info("job %s: %s", job["id"], status)
