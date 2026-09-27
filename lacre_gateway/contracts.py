"""The Lacre contracts as the gateway reads them: always through the Router.

No Verifier or KeyCache address is configured or cached. Each question
resolves "verifier" or "keycache" on the Router at LATEST_FINAL first, as
Verifier v1.2 itself does for the KeyCache, so a change applied on the
Router after its 48 hour delay is followed without a restart.
"""

import datetime

from .chainio import ChainUnavailable

STATES = ("active", "pending", "rotated", "retired")


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
