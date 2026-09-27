"""Test doubles: a chain that answers from recorded fixtures, never the network.

The consensus fixtures are copied unchanged from vendor/lacre/tests/fixtures
and decoded with txstate.decode_stored, as the public repo's own tests do:

  consensus_call36_timeout.json      FINALIZED, result TIMEOUT: not executed
  consensus_call37_agree.json        FINALIZED, AGREE, FINISHED_WITH_RETURN
  consensus_verifier_underpaid.json  FINALIZED, AGREE, executed and refused
                                     with a refund to the sender

States the fixtures do not hold (ACCEPTED, CANCELED, an appeal, an
undecided PENDING) are the recorded ones with the status changed, the same
way vendor/lacre/tests/test_attest.py builds them.
"""

import base64
import json
import re
from pathlib import Path

from genlayer_py.chains import testnet_bradbury

from lacre_gateway.chainio import ChainUnavailable, NothingSent, Stop
from lacre_gateway.config import Settings
from lacre_gateway.vendor import dkimcore, txstate

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ABI = testnet_bradbury.consensus_data_contract["abi"]

ROUTER = "0x" + "a1" * 20
VERIFIER = "0x" + "b2" * 20
VERIFIER_OLD = "0x" + "b1" * 20
KEYCACHE = "0x" + "c3" * 20
REQUESTER = "0x" + "d4" * 20
API_KEY = "test-key-" + "x" * 24
EXPLORER = "https://explorer-bradbury.genlayer.com"


def recorded(name):
    """(stored state, messages) of a recorded consensus fixture."""
    raw = json.loads((FIXTURES / name).read_text(encoding="ascii"))
    transaction, rounds = raw["getTransactionAllData"]
    transaction = list(transaction)
    index = txstate.fields(ABI, "getTransactionAllData").index("eqBlocksOutputs")
    transaction[index] = bytes.fromhex(transaction[index][2:])
    state = txstate.decode_stored(ABI, [transaction, rounds])
    return state, txstate.decode_messages(ABI, raw["getTransactionData_messages"])


TIMEOUT, _ = recorded("consensus_call36_timeout.json")
AGREE, _ = recorded("consensus_call37_agree.json")
UNDERPAID, REFUND = recorded("consensus_verifier_underpaid.json")
PENDING = dict(AGREE, status="PENDING", result="IDLE", execution="NOT_VOTED")
ACCEPTED = dict(AGREE, status="ACCEPTED")
CANCELED = dict(AGREE, status="CANCELED")
APPEALED = dict(AGREE, status="APPEAL_COMMITTING")
UNDETERMINED = dict(TIMEOUT, status="UNDETERMINED", result="NO_MAJORITY")
RECORD = json.loads((FIXTURES / "verifier_record.json").read_text(encoding="ascii"))


def settings(tmp_path, **changes):
    values = dict(network="bradbury", router=ROUTER, api_keys=(API_KEY,),
                  blob_base_url="https://gateway.example.org/h", data_dir=Path(tmp_path),
                  poll_s=30, final_bound_s=4 * 3600, max_attempts=3,
                  max_send_failures=3, key_quarantine_s=24 * 3600,
                  confirm_margin_s=600, confirm_retry_s=3600, worker_stale_s=300)
    values.update(changes)
    return Settings(**values)


class Clock:
    def __init__(self, start=1_790_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeChain:
    """The contracts and ConsensusData as views over plain data.

    It also plays the network's part of rule 13: a send to a contract that
    still has an undecided transaction of ours fails the test.
    """

    def __init__(self):
        self.can_sign = True
        self.requester = REQUESTER
        self.up = True
        self.resolves = {"verifier": VERIFIER, "keycache": KEYCACHE}
        self.history = [VERIFIER_OLD, VERIFIER]
        self.keys = {}
        self.failures = {}
        self.fee = 0
        self.records = {VERIFIER: {}, VERIFIER_OLD: {}}
        self.refusal = {VERIFIER: ""}
        self.sent = []
        self.states = {}
        self.fail_next_send = []
        self.on_final = {}
        self.reads = []
        # The last stored state the gateway was shown, per tx.
        self.current = {}

    # ---- programming -----------------------------------------------------

    def script(self, tx_id, *states):
        """Stored states tx_id goes through, one per read; the last repeats."""
        self.states[tx_id] = list(states)

    def next_tx(self):
        return "0x%064x" % (len(self.sent) + 1,)

    def add_record(self, verifier=VERIFIER, **fields):
        book = self.records[verifier]
        record_id = str(len(book))
        record = dict(RECORD, id=record_id, requester=self.requester, source="url",
                      fee_paid=str(self.fee))
        record.update(fields)
        book[record_id] = record
        return record_id

    # ---- the Chain interface ---------------------------------------------

    def ping(self):
        return self.up

    def explorer(self, tx_id):
        return "%s/tx/%s" % (EXPLORER, tx_id)

    def view(self, address, method, args, final=True):
        if not self.up:
            raise ChainUnavailable("down")
        self.reads.append((address, method, tuple(args), final))
        if address == ROUTER and method == "resolve":
            return self.resolves.get(args[0], "")
        if address == ROUTER and method == "history":
            return [{"version": "v%d" % i, "address": a, "set_at": "x"}
                    for i, a in enumerate(self.history)]
        if address == KEYCACHE and method == "key_status":
            return dict(self.keys.get((args[0], args[1]), {}))
        if address == KEYCACHE and method == "last_failure":
            return self.failures.get((args[0], args[1]), "")
        if address in self.records:
            book = self.records[address]
            if method == "fee":
                return self.fee
            if method == "records_of":
                return [i for i, r in book.items()
                        if r["requester"].lower() == args[0].lower()]
            if method == "last_refusal":
                return self.refusal.get(address, "")
            if method == "get":
                return dict(book.get(str(args[0]), {}))
        raise AssertionError("unexpected read %r" % ((address, method, args),))

    def send(self, address, method, args, value=0):
        for tx_id, (to, _, _, _) in self.tx_to.items():
            if to.lower() == address.lower():
                state = self.current.get(tx_id, PENDING)
                assert state["status"] in txstate.DECIDED and \
                    state["status"] not in txstate.APPEAL, \
                    "a second call to %s while %s is %s" % (address, tx_id, state["status"])
        if self.fail_next_send:
            raise self.fail_next_send.pop(0)
        tx_id = self.next_tx()
        self.sent.append((tx_id, address, method, list(args), value))
        self.states.setdefault(tx_id, [PENDING])
        return tx_id

    @property
    def tx_to(self):
        return {tx: (to, m, a, v) for tx, to, m, a, v in self.sent}

    def stored(self, tx_id):
        if not self.up:
            raise ChainUnavailable("down")
        queue = self.states[tx_id]
        state = queue.pop(0) if len(queue) > 1 else queue[0]
        self.current[tx_id] = state
        if state["status"] == "FINALIZED" and tx_id in self.on_final:
            self.on_final.pop(tx_id)()
        return state

    def sends(self, method=None):
        return [s for s in self.sent if method is None or s[2] == method]


def rsa_key(bits=1024):
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def sign_eml(raw, domain, key):
    """raw with the b= of the DKIM-Signature for domain replaced by a real
    signature from key, computed with the Verifier's own signed_data."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    head, sep, body = raw.partition(b"\r\n\r\n")
    fields = dkimcore.parse_headers(head)
    for index, (name, value) in enumerate(fields):
        tags = dkimcore.parse_tags(value.decode("latin-1"))
        if dkimcore.field_name(name) == b"dkim-signature" and tags.get("d") == domain:
            break
    else:
        raise AssertionError("no signature for %s" % (domain,))
    names = [n.strip() for n in tags["h"].split(":") if n.strip()]
    data = dkimcore.signed_data(fields, index, names)
    signature = base64.b64encode(key.sign(data, padding.PKCS1v15(), hashes.SHA256()))
    placeholder = re.search(rb"b=(SIGNATUREPLACEHOLDER\w+)", value).group(1)
    return head.replace(placeholder, signature) + sep + body


def public_numbers(key):
    numbers = key.public_key().public_numbers()
    return numbers.n, numbers.e


def amazon_eml():
    return (FIXTURES / "amazon_layout.eml").read_bytes()


def active_key(domain="amazon.com", selector="synthsel2026a"):
    return {"domain": domain, "selector": selector, "state": "active", "n_hex": "c3" * 128,
            "e": "65537", "key_bits": "1024", "key_sha256": "ab" * 32,
            "first_seen": "2026-09-20T00:00:00Z", "activated_at": "2026-09-21T00:10:00Z",
            "refreshed_at": "2026-09-21T00:10:00Z"}


def submit(gw, raw=None):
    """What POST /attest does, without HTTP: (job id, Selection)."""
    from lacre_gateway import headers

    chosen = headers.select(raw if raw is not None else amazon_eml())
    sender = gw.store.sender(chosen.domain, chosen.selector)
    job_id = gw.store.create_job(sender["id"], chosen.headers_sha256, chosen.bh,
                                 chosen.body_hash_ok)
    gw.blobs.stage(job_id, chosen.blob)
    return job_id, chosen


def our_record(gw, chosen, **fields):
    """A record the job's call would write, on the current Verifier."""
    values = dict(domain=chosen.domain, selector=chosen.selector, bh=chosen.bh)
    values.update(fields)
    return gw.chain.add_record(**values)


__all__ = ["NothingSent", "Stop"]
