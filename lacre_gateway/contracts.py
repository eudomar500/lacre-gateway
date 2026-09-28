"""The Lacre contracts as the gateway reads them: always through the Router.

No Verifier, KeyCache or Extractor address is configured or cached. Each
question resolves "verifier", "keycache", "extractor" or "extractor_llm" on
the Router at LATEST_FINAL first, as Verifier v1.2 itself does for the
KeyCache, so a change applied on the Router after its 48 hour delay is
followed without a restart. A job keeps only the address its own call went
to, because that is the contract whose records answer for the call.
"""

import datetime

from .chainio import ChainUnavailable
from .vendor import extract

STATES = ("active", "pending", "rotated", "retired")
# The Router name of each extraction lane (tools/extract.py LANES).
LANES = extract.LANES
# What a job keeps of its extraction record: the fields that say what the
# body said and how it was read. The rest (requester, bh, domain, times) is
# on chain, and GET /extractions reads it there.
EXTRACTION_FIELDS = ("match", "shipped", "eta_day", "eta_date", "order_id_found", "flagged",
                     "method", "patterns_sha256", "prompt_sha256", "reason", "signed_at")


def unix_time(text):
    """Seconds since the epoch of a runner datetime such as first_seen."""
    value = datetime.datetime.fromisoformat(str(text).strip().replace(" ", "T", 1))
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.timestamp()


class Contracts:
    def __init__(self, chain, router):
        self.chain = chain
        self.router = router

    def resolve(self, name):
        address = self.chain.view(self.router, "resolve", [name], final=True)
        if not address:
            raise ChainUnavailable("the Router resolves no %s" % (name,))
        return str(address)

    def verifier(self):
        return self.resolve("verifier")

    def extractor(self, lane):
        """The lane's Extractor, or "" when the Router names none."""
        return str(self.chain.view(self.router, "resolve", [LANES[lane]], final=True) or "")

    def has_patterns(self, extractor, domain):
        # Read at LATEST_NONFINAL, as extract.refusal reads it: the document
        # a call is checked against is the one in force when it executes.
        return bool(self.chain.view(extractor, "patterns", [domain], final=False))

    def extraction(self, extractor, record_id):
        return self.chain.view(extractor, "get_record", [str(record_id)], final=True) or {}

    def keycache(self):
        return self.resolve("keycache")

    def verifier_history(self):
        """Every address "verifier" has named on the Router, oldest first."""
        entries = self.chain.view(self.router, "history", ["verifier"], final=True) or []
        return [str(entry.get("address", "")) for entry in entries]

    def key_status(self, domain, selector):
        """KeyCache.key_status at LATEST_FINAL, the state the Verifier reads."""
        return self.chain.view(self.keycache(), "key_status", [domain, selector],
                               final=True) or {}

    def last_failure(self, domain, selector):
        text = str(self.chain.view(self.keycache(), "last_failure", [domain, selector],
                                   final=True) or "")
        # The KeyCache prefixes the runner datetime; the reason is the rest.
        return text.split(" ", 1)[1] if " " in text else text

    def fee(self, verifier):
        # attest.refusal reads fee() at LATEST_NONFINAL too: the fee a call
        # is checked against is the one in force when it executes.
        return int(self.chain.view(verifier, "fee", [], final=False))

    def records_of(self, verifier, requester):
        return [str(i) for i in (self.chain.view(verifier, "records_of", [requester],
                                                 final=True) or [])]

    def last_refusal(self, verifier, requester):
        return str(self.chain.view(verifier, "last_refusal", [requester], final=True) or "")

    def record(self, verifier, record_id):
        return self.chain.view(verifier, "get", [str(record_id)], final=True) or {}


def sender_state(status):
    """unknown | pending | active | rotated | retired, from key_status."""
    state = str(status.get("state", "")) if status else ""
    return state if state in STATES else "unknown"


def confirm_after(status, quarantine_s):
    """When confirm_key may first be sent for a pending key, unix seconds."""
    return unix_time(status["first_seen"]) + quarantine_s
