"""Where credits come from.

A TopUpSource turns one verified external event into credits on one
account. The event is named by (source, external_ref), and the store
credits a pair once, so a source may report the same event as often as its
own delivery repeats it. Verifying the event (a payment provider's
signature, a deposit read at a final block) is the source's job and happens
before credit is called.

SOURCES is the registry: the one place a new source is added, under the
name its top-ups carry in the source column. Only ManualTopUp exists now,
driven by POST /admin/accounts/{id}/topups, where the operator is the
verification. A payment webhook or a watcher of GEN deposits would be a
TopUpSource subclass added here, with its own entry point (an HTTP route or
a worker step) calling credit.
"""


class TopUpSource:
    name = None

    def __init__(self, store):
        self.store = store

    def credit(self, account_id, credits, external_ref, note=None):
        """(topup row, created) for one verified event.

        created is False when the event was already credited; the row is the
        one made then. Raises store.TopUpConflict when the event was
        credited to another account or amount.
        """
        if credits <= 0:
            raise ValueError("a top-up adds at least one credit")
        if not external_ref:
            raise ValueError("a top-up names the event it comes from")
        return self.store.topup(self.name, account_id, credits, external_ref, note)


class ManualTopUp(TopUpSource):
    """Credits the operator grants, for payments settled outside the gateway.
    external_ref is the operator's reference for it: an invoice or receipt."""

    name = "manual"


SOURCES = {ManualTopUp.name: ManualTopUp}
