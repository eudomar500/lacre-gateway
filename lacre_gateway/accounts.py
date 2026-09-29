"""API keys as accounts.

A key is looked up by its SHA-256, the only form of it the store keeps. The
lookup compares digests, so how long it takes says nothing about how close
a wrong key is to a right one.

LACRE_API_KEYS is a bootstrap list. On start every entry that has no account
gets one, named bootstrap-N, with LACRE_BOOTSTRAP_CREDITS. While that is 0
those accounts are unlimited: a job for them is priced and settled like any
other, but nothing is taken from a balance. That keeps a gateway deployed
before accounts working unchanged across the upgrade, and is meant for
testnet only.
"""

import hashlib
import logging
import secrets

log = logging.getLogger("lacre_gateway.accounts")

# 32 random bytes, as the deploy notes generate LACRE_API_KEYS entries.
KEY_BYTES = 32


def digest(api_key):
    return hashlib.sha256(api_key.encode()).hexdigest()


def new_key():
    return secrets.token_urlsafe(KEY_BYTES)


def authenticate(store, given):
    """The account the key given belongs to, or None."""
    if not given:
        return None
    return store.account_by_key(digest(given))


def bootstrap(store, keys, credits):
    """Accounts for the LACRE_API_KEYS entries, then the move of rows from
    before accounts to them, then the ledger check.

    An entry is matched on the digest it had when its account was made, so
    an account whose key was rotated is not given a second account for the
    old entry. unlimited follows the configuration on every start: an
    account stays unlimited only while its entry is listed and credits is
    0. An entry taken out of the list revokes its key, as removing it did
    before accounts, unless the key was rotated since, in which case the
    account just stops being unlimited.
    """
    listed = {digest(k) for k in keys}
    known = {a["bootstrap_sha256"]: a for a in store.bootstrap_accounts()}
    for key in keys:
        entry = digest(key)
        if entry in known:
            continue
        if store.account_by_key(entry) is not None:
            # An admin-made account can never share a digest with an entry
            # unless someone copied its key into the list; it is left alone.
            log.warning("a LACRE_API_KEYS entry is the key of an existing account, skipped")
            continue
        number = len(store.bootstrap_accounts()) + 1
        account = store.create_account("bootstrap-%d" % (number,), entry, credits=credits,
                                       unlimited=credits == 0, bootstrap_sha256=entry,
                                       note="bootstrap credits")
        log.info("account %s created for a LACRE_API_KEYS entry", account["id"])
    for account in store.bootstrap_accounts():
        entry = account["bootstrap_sha256"]
        unlimited = int(entry in listed and credits == 0)
        if account["unlimited"] != unlimited:
            store.update_account(account["id"], unlimited=unlimited)
        if entry not in listed and account["key_sha256"] == entry and account["enabled"]:
            store.set_enabled(account["id"], False)
            log.info("account %s disabled: its key is no longer in LACRE_API_KEYS",
                     account["id"])
    store.migrate_owners()
    store.check_ledger()
