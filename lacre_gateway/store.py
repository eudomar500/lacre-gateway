"""SQLite persistence: accounts and their ledger, jobs, senders, the
in-flight table, a heartbeat and access requests.

What is stored is listed column by column below, and nothing else is. No
email address, subject, header value or body is ever written here. The two
values taken from a message are the signing domain and selector (d= and s=),
because every KeyCache and Verifier call takes them as public calldata
arguments and a job may have to wait a day for its sender key; they name the
sender's mail system, not a person. bh and the blob digest are hashes. Of an
extraction, only the fields the Extractor stored on chain are kept, and they
are public there already: booleans, a weekday, a date, digests and a fixed
reason phrase. No order number is ever among them.

An account row holds its random id, a name the operator chose, the SHA-256
of its API key (never the key) and its credit balance. The balance is a
cache: the ledger is the record, one row per change, and credits is its sum
(check_ledger). A job holds credits when it is created and settles when it
ends; its hold, what was charged and what was released are kept on the job.

A mailbox row holds its random id, the account that owns it, its extract
mode and counters. Mail that arrives there goes through the same path as an
upload: nothing of it is written here but what a job keeps, plus the mailbox
id the job came through. The addresses of the sender and of any other
recipient are never stored. The inbound replay table holds HMAC values only,
for the length of the replay window.

The one exception to the rule on addresses is access_requests: a person who
asks for access through the web form gives a name, an email address and a
line on what they will attest, so the operator can answer. Those three
fields and the time are all a row holds; the visitor's IP address is not
stored, only counted in memory for the rate limit.
"""

import base64
import contextlib
import json
import logging
import secrets
import sqlite3
import threading
import time
import uuid

log = logging.getLogger("lacre_gateway.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS senders (
    id            INTEGER PRIMARY KEY,
    domain        TEXT NOT NULL,
    selector      TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'unknown',
    op            TEXT,
    op_tx         TEXT,
    op_sent_at    REAL,
    op_attempts   INTEGER NOT NULL DEFAULT 0,
    confirm_after REAL,
    next_at       REAL NOT NULL DEFAULT 0,
    reason        TEXT,
    updated_at    REAL NOT NULL,
    UNIQUE (domain, selector)
);
CREATE TABLE IF NOT EXISTS jobs (
    id             TEXT PRIMARY KEY,
    status         TEXT NOT NULL,
    stage          TEXT NOT NULL,
    sender_id      INTEGER NOT NULL REFERENCES senders(id),
    headers_sha256 TEXT NOT NULL,
    bh             TEXT NOT NULL,
    body_hash_ok   INTEGER,
    blob_token     TEXT,
    verifier       TEXT,
    value_wei      TEXT,
    records_before TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    send_failures  INTEGER NOT NULL DEFAULT 0,
    tx_ids         TEXT NOT NULL DEFAULT '[]',
    current_tx     TEXT,
    tx_status      TEXT,
    record_id      TEXT,
    valid_aligned  INTEGER,
    refusal_reason TEXT,
    error          TEXT,
    confirm_after  REAL,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    submitted_at   REAL,
    decided_at     REAL,
    finished_at    REAL
);
-- One row per contract with a call of ours that is not decided yet
-- (docs/interfaces.md section 5, rule 13). The primary key is the rule.
CREATE TABLE IF NOT EXISTS inflight (
    contract TEXT PRIMARY KEY,
    owner    TEXT NOT NULL,
    tx_id    TEXT,
    since    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- account_id is NULL only for a mailbox migrated from a key that no
-- account took over (Store.migrate_owners); no key reaches it.
CREATE TABLE IF NOT EXISTS mailboxes (
    id               TEXT PRIMARY KEY,
    account_id       TEXT,
    extract_mode     TEXT NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1,
    received         INTEGER NOT NULL DEFAULT 0,
    dropped          INTEGER NOT NULL DEFAULT 0,
    last_received_at REAL,
    created_at       REAL NOT NULL,
    disabled_at      REAL
);
-- unlimited is set only on accounts made for LACRE_API_KEYS entries while
-- LACRE_BOOTSTRAP_CREDITS is 0; such an account is never held against.
-- bootstrap_sha256 is the digest of the LACRE_API_KEYS entry the account
-- was made for, kept after a key rotation so the entry is not taken for a
-- new key on the next start.
CREATE TABLE IF NOT EXISTS accounts (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    key_sha256       TEXT NOT NULL UNIQUE,
    credits          INTEGER NOT NULL DEFAULT 0 CHECK (credits >= 0),
    unlimited        INTEGER NOT NULL DEFAULT 0,
    bootstrap_sha256 TEXT UNIQUE,
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       REAL NOT NULL,
    disabled_at      REAL
);
CREATE TABLE IF NOT EXISTS ledger (
    id         INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    delta      INTEGER NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('hold', 'settle', 'release', 'topup', 'adjust')),
    job_id     TEXT,
    topup_id   TEXT,
    note       TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_account ON ledger (account_id, id);
-- The pair (source, external_ref) is the event a top-up came from; the
-- same event credits once however often it is reported.
CREATE TABLE IF NOT EXISTS topups (
    id           TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL REFERENCES accounts(id),
    credits      INTEGER NOT NULL CHECK (credits > 0),
    source       TEXT NOT NULL,
    external_ref TEXT NOT NULL,
    note         TEXT,
    created_at   REAL NOT NULL,
    UNIQUE (source, external_ref)
);
-- Signatures of inbound deliveries already taken, so the same signed
-- request cannot create a second job while its timestamp is still fresh.
CREATE TABLE IF NOT EXISTS inbound_seen (
    signature TEXT PRIMARY KEY,
    seen_at   REAL NOT NULL
);
-- Requests from the access form of the web app, kept until the operator
-- deletes them by hand.
CREATE TABLE IF NOT EXISTS access_requests (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    email      TEXT NOT NULL,
    what       TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

# Columns added after v0, by ALTER TABLE on a database that lacks them, so
# a deployed gateway keeps its jobs across the upgrade. A v0 job reads as
# extract_mode NULL, which is treated as "none".
EXTRACTION_COLUMNS = (
    ("extract_mode", "TEXT"),
    # skipped, extracted, no match, refused, failed; NULL while undecided.
    ("ext_status", "TEXT"),
    ("ext_note", "TEXT"),
    ("ext_lane", "TEXT"),
    ("extractor", "TEXT"),
    ("body_name", "TEXT"),
    ("ext_before", "TEXT"),
    ("ext_value_wei", "TEXT"),
    ("ext_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("ext_send_failures", "INTEGER NOT NULL DEFAULT 0"),
    ("ext_tx_ids", "TEXT NOT NULL DEFAULT '[]'"),
    ("ext_current_tx", "TEXT"),
    ("ext_tx_status", "TEXT"),
    ("ext_submitted_at", "REAL"),
    ("ext_record_id", "TEXT"),
    ("ext_record", "TEXT"),
)
# Added with mailboxes. A job from before them reads as via NULL, which is
# treated as "api".
MAILBOX_COLUMNS = (
    # api or inbound.
    ("via", "TEXT"),
    ("mailbox", "TEXT"),
)
# Added with accounts. A job from before them has no account and was never
# held against, and settles to nothing.
ACCOUNT_COLUMNS = (
    ("account_id", "TEXT"),
    # 1 when the hold went to the ledger; 0 for an unlimited account, whose
    # job is priced but takes nothing from the balance.
    ("metered", "INTEGER NOT NULL DEFAULT 0"),
    ("held_attest", "INTEGER NOT NULL DEFAULT 0"),
    ("held_extract", "INTEGER NOT NULL DEFAULT 0"),
    # NULL until the job ends and its hold is settled.
    ("charged", "INTEGER"),
    ("released", "INTEGER"),
    ("settled_at", "REAL"),
)
JSON_COLUMNS = ("tx_ids", "records_before", "ext_tx_ids", "ext_before", "ext_record")

OPEN = ("pending", "attesting", "extracting")
TERMINAL = ("finalized", "refused", "failed")


class InsufficientCredits(Exception):
    def __init__(self, needed, credits):
        super().__init__("needed %d, have %d" % (needed, credits))
        self.needed = needed
        self.credits = credits
        self.shortfall = needed - credits


class TopUpConflict(Exception):
    """The event was already credited, to another account or amount."""


class LedgerError(Exception):
    pass


def random_id():
    # 8 random bytes make 13 base32 characters; the first 12 carry 60
    # uniformly random bits.
    return base64.b32encode(secrets.token_bytes(8)).decode("ascii")[:12].lower()


class Store:
    def __init__(self, path, clock=time.time):
        self.clock = clock
        self._lock = threading.RLock()
        self._depth = 0
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            if str(path) != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            have = self._columns("jobs")
            for name, kind in EXTRACTION_COLUMNS + MAILBOX_COLUMNS + ACCOUNT_COLUMNS:
                if name not in have:
                    self._db.execute("ALTER TABLE jobs ADD COLUMN %s %s" % (name, kind))
            if "account_id" not in self._columns("mailboxes"):
                self._db.execute("ALTER TABLE mailboxes ADD COLUMN account_id TEXT")
            self._db.execute("CREATE INDEX IF NOT EXISTS jobs_mailbox ON jobs (mailbox) "
                             "WHERE mailbox IS NOT NULL")
            self._db.execute("CREATE INDEX IF NOT EXISTS jobs_account ON jobs (account_id) "
                             "WHERE account_id IS NOT NULL")
            self._db.execute("CREATE INDEX IF NOT EXISTS mailboxes_account "
                             "ON mailboxes (account_id)")
            # v0's partial index left extracting jobs out.
            self._db.execute("DROP INDEX IF EXISTS jobs_open")
            self._db.execute("CREATE INDEX IF NOT EXISTS jobs_active ON jobs (status) "
                             "WHERE status IN ('pending', 'attesting', 'extracting')")

    def _columns(self, table):
        return {r[1] for r in self._db.execute("PRAGMA table_info(%s)" % (table,))}

    @contextlib.contextmanager
    def _tx(self):
        """One transaction; nested uses join the outer one.

        Every change to a balance writes its ledger row in the same
        transaction, so the two cannot disagree after a crash.
        """
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self._db.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")
            finally:
                self._depth = 0

    def _one(self, sql, args=()):
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _run(self, sql, args=()):
        with self._lock:
            return self._db.execute(sql, args)

    # ---- senders ---------------------------------------------------------

    def sender(self, domain, selector):
        """The sender row, created on first sight."""
        with self._lock:
            self._run("INSERT OR IGNORE INTO senders (domain, selector, updated_at) "
                      "VALUES (?, ?, ?)", (domain, selector, self.clock()))
            return self._one("SELECT * FROM senders WHERE domain = ? AND selector = ?",
                             (domain, selector))

    def sender_by_id(self, sender_id):
        return self._one("SELECT * FROM senders WHERE id = ?", (sender_id,))

    def update_sender(self, sender_id, **values):
        values["updated_at"] = self.clock()
        cols = ", ".join("%s = ?" % k for k in values)
        self._run("UPDATE senders SET %s WHERE id = ?" % cols, (*values.values(), sender_id))

    def waiting_senders(self):
        """Senders at least one open job is waiting on."""
        return self._all(
            "SELECT DISTINCT s.* FROM senders s JOIN jobs j ON j.sender_id = s.id "
            "WHERE j.status = 'pending' ORDER BY s.id")

    # ---- jobs --------------------------------------------------------------

    def create_job(self, sender_id, headers_sha256, bh, body_hash_ok, extract_mode="none",
                   skipped=None, via="api", mailbox=None, account_id=None, held_attest=0,
                   held_extract=0):
        """A new pending job. skipped is the reason an extraction that was
        asked for cannot run, known already at upload.

        With an account, held_attest plus held_extract is taken from its
        balance in the same transaction as the insert, or InsufficientCredits
        is raised and nothing is written.
        """
        now = self.clock()
        job_id = uuid.uuid4().hex
        held = held_attest + held_extract
        with self._tx():
            metered = False
            if account_id is not None:
                account = self.account(account_id)
                metered = not account["unlimited"]
                if metered and account["credits"] < held:
                    raise InsufficientCredits(held, account["credits"])
            self._run(
                "INSERT INTO jobs (id, status, stage, sender_id, headers_sha256, bh, "
                "body_hash_ok, extract_mode, ext_status, ext_note, via, mailbox, account_id, "
                "metered, held_attest, held_extract, created_at, updated_at) "
                "VALUES (?, 'pending', 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, sender_id, headers_sha256, bh,
                 None if body_hash_ok is None else int(body_hash_ok), extract_mode,
                 "skipped" if skipped else None, skipped, via, mailbox, account_id,
                 int(metered), held_attest, held_extract, now, now))
            if metered and held:
                self._entry(account_id, -held, "hold", job_id=job_id,
                            note="attest %d, extract %d" % (held_attest, held_extract))
        return job_id

    def job(self, job_id):
        row = self._one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if row:
            for key in JSON_COLUMNS:
                row[key] = json.loads(row[key]) if row[key] is not None else None
        return row

    def open_jobs(self):
        ids = self._all("SELECT id FROM jobs WHERE status IN ('pending', 'attesting', "
                        "'extracting') ORDER BY created_at")
        return [self.job(r["id"]) for r in ids]

    def update_job(self, job_id, **values):
        values["updated_at"] = self.clock()
        for key in JSON_COLUMNS:
            if key in values and values[key] is not None:
                values[key] = json.dumps(values[key])
        ending = values.get("status") in TERMINAL
        if ending:
            values.setdefault("finished_at", values["updated_at"])
        cols = ", ".join("%s = ?" % k for k in values)
        with self._tx():
            self._run("UPDATE jobs SET %s WHERE id = ?" % cols, (*values.values(), job_id))
            # Settled here, where a job becomes terminal, rather than at each
            # place that ends one, so no path can leave credits held.
            if ending:
                self._settle(job_id)

    def _settle(self, job_id):
        """Charge the hold for the steps that wrote a record, release the rest.

        A Verifier record charges attest, valid or not; an extraction record
        charges extract, match false included, since the Extractor kept its
        fee for it. A refusal, a failure or a skipped extraction wrote no
        record, and its part is released.
        """
        job = self._one("SELECT account_id, metered, held_attest, held_extract, record_id, "
                        "ext_record_id, settled_at FROM jobs WHERE id = ?", (job_id,))
        if job is None or job["settled_at"] is not None:
            return
        attest = job["held_attest"] if job["record_id"] is not None else 0
        extract = job["held_extract"] if job["ext_record_id"] is not None else 0
        held = job["held_attest"] + job["held_extract"]
        charged = attest + extract
        released = held - charged
        self._run("UPDATE jobs SET charged = ?, released = ?, settled_at = ? WHERE id = ?",
                  (charged, released, self.clock(), job_id))
        if not job["metered"] or not held:
            return
        # The charge is the part of the hold that is not given back, so the
        # settle row moves nothing; it records what the hold turned into.
        self._entry(job["account_id"], 0, "settle", job_id=job_id,
                    note="charged %d: attest %d, extract %d" % (charged, attest, extract))
        if released:
            self._entry(job["account_id"], released, "release", job_id=job_id)

    def claimed_records(self, verifier, other_than):
        """Record ids on verifier already taken by a job other than other_than."""
        rows = self._all("SELECT record_id FROM jobs WHERE lower(verifier) = lower(?) "
                         "AND record_id IS NOT NULL AND id != ?", (verifier, other_than))
        return {r["record_id"] for r in rows}

    def mailbox_jobs(self, mailbox_id, limit, offset):
        """A page of the jobs that came through mailbox_id, newest first."""
        # rowid breaks ties between jobs created in the same clock tick, so
        # pages do not overlap or skip.
        ids = self._all("SELECT id FROM jobs WHERE mailbox = ? "
                        "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                        (mailbox_id, limit, offset))
        return [self.job(r["id"]) for r in ids]

    def account_jobs(self, account_id, statuses, limit, offset):
        """A page of account_id's jobs, newest first: id, status, stage and
        the times only. statuses narrows it to those statuses; None is all."""
        where, args = "account_id = ?", [account_id]
        if statuses:
            where += " AND status IN (%s)" % (", ".join("?" * len(statuses)),)
            args.extend(statuses)
        return self._all("SELECT id, status, stage, created_at, updated_at, finished_at "
                         "FROM jobs WHERE %s ORDER BY created_at DESC, rowid DESC "
                         "LIMIT ? OFFSET ?" % (where,), (*args, limit, offset))

    # ---- mailboxes -----------------------------------------------------------

    def create_mailbox(self, account_id, extract_mode):
        """A new enabled mailbox with a fresh random id."""
        now = self.clock()
        for _ in range(5):
            mailbox_id = random_id()
            try:
                self._run("INSERT INTO mailboxes (id, account_id, extract_mode, created_at) "
                          "VALUES (?, ?, ?, ?)", (mailbox_id, account_id, extract_mode, now))
            except sqlite3.IntegrityError:
                continue
            return self.mailbox(mailbox_id)
        raise RuntimeError("no free mailbox id after 5 draws")

    def mailbox(self, mailbox_id):
        return self._one("SELECT * FROM mailboxes WHERE id = ?", (mailbox_id,))

    def mailboxes(self, account_id):
        return self._all("SELECT * FROM mailboxes WHERE account_id = ? "
                         "ORDER BY created_at, rowid", (account_id,))

    def disable_mailbox(self, mailbox_id):
        self._run("UPDATE mailboxes SET enabled = 0, disabled_at = ? "
                  "WHERE id = ? AND enabled = 1", (self.clock(), mailbox_id))

    def count_inbound(self, mailbox_id, received):
        if received:
            self._run("UPDATE mailboxes SET received = received + 1, last_received_at = ? "
                      "WHERE id = ?", (self.clock(), mailbox_id))
        else:
            self._run("UPDATE mailboxes SET dropped = dropped + 1 WHERE id = ?", (mailbox_id,))

    def count_unknown_inbound(self):
        """Mail to a mailbox id that does not exist has no row to count on."""
        with self._lock:
            self._run("INSERT OR IGNORE INTO meta (key, value) VALUES ('inbound_unknown', '0')")
            self._run("UPDATE meta SET value = CAST(value AS INTEGER) + 1 "
                      "WHERE key = 'inbound_unknown'")

    def unknown_inbound(self):
        row = self._one("SELECT value FROM meta WHERE key = 'inbound_unknown'")
        return int(row["value"]) if row else 0

    def remember_signature(self, signature, keep_s):
        """True the first time signature is seen within keep_s, else False."""
        now = self.clock()
        with self._lock:
            self._run("DELETE FROM inbound_seen WHERE seen_at < ?", (now - keep_s,))
            try:
                self._run("INSERT INTO inbound_seen (signature, seen_at) VALUES (?, ?)",
                          (signature, now))
            except sqlite3.IntegrityError:
                return False
        return True

    # ---- accounts ----------------------------------------------------------

    def _entry(self, account_id, delta, kind, job_id=None, topup_id=None, note=None):
        """A ledger row and the balance it moves, inside the caller's
        transaction. The CHECK on credits refuses a balance below zero."""
        self._run("INSERT INTO ledger (account_id, delta, kind, job_id, topup_id, note, "
                  "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (account_id, delta, kind, job_id, topup_id, note, self.clock()))
        if delta:
            self._run("UPDATE accounts SET credits = credits + ? WHERE id = ?",
                      (delta, account_id))

    def create_account(self, name, key_sha256, credits=0, unlimited=False,
                       bootstrap_sha256=None, note="initial credits"):
        """A new enabled account; credits above 0 go in as an adjust row."""
        now = self.clock()
        with self._tx():
            for _ in range(5):
                account_id = random_id()
                if self.account(account_id) is None:
                    break
            else:
                raise RuntimeError("no free account id after 5 draws")
            self._run("INSERT INTO accounts (id, name, key_sha256, unlimited, bootstrap_sha256, "
                      "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                      (account_id, name, key_sha256, int(unlimited), bootstrap_sha256, now))
            if credits:
                self._entry(account_id, credits, "adjust", note=note)
        return self.account(account_id)

    def account(self, account_id):
        return self._one("SELECT * FROM accounts WHERE id = ?", (account_id,))

    def account_by_key(self, key_sha256):
        return self._one("SELECT * FROM accounts WHERE key_sha256 = ?", (key_sha256,))

    def accounts(self):
        return self._all("SELECT * FROM accounts ORDER BY created_at, rowid")

    def bootstrap_accounts(self):
        return self._all("SELECT * FROM accounts WHERE bootstrap_sha256 IS NOT NULL "
                         "ORDER BY created_at, rowid")

    def update_account(self, account_id, **values):
        cols = ", ".join("%s = ?" % k for k in values)
        self._run("UPDATE accounts SET %s WHERE id = ?" % cols, (*values.values(), account_id))

    def set_enabled(self, account_id, enabled):
        if enabled:
            self._run("UPDATE accounts SET enabled = 1, disabled_at = NULL "
                      "WHERE id = ? AND enabled = 0", (account_id,))
        else:
            self._run("UPDATE accounts SET enabled = 0, disabled_at = ? "
                      "WHERE id = ? AND enabled = 1", (self.clock(), account_id))

    def ledger(self, account_id, limit):
        """The newest ledger rows of an account, newest first."""
        return self._all("SELECT * FROM ledger WHERE account_id = ? ORDER BY id DESC LIMIT ?",
                         (account_id, limit))

    def job_ledger(self, job_id):
        return self._all("SELECT * FROM ledger WHERE job_id = ? ORDER BY id", (job_id,))

    def usage(self, account_id):
        """Job counts by status, credits priced into open jobs and charged
        by finished ones, and the number of mailboxes."""
        counts = {s: 0 for s in OPEN + TERMINAL}
        for row in self._all("SELECT status, count(*) AS n FROM jobs WHERE account_id = ? "
                             "GROUP BY status", (account_id,)):
            counts[row["status"]] = row["n"]
        sums = self._one(
            "SELECT coalesce(sum(CASE WHEN settled_at IS NULL "
            "THEN held_attest + held_extract END), 0) AS held, "
            "coalesce(sum(charged), 0) AS charged FROM jobs WHERE account_id = ?",
            (account_id,))
        boxes = self._one("SELECT count(*) AS n FROM mailboxes WHERE account_id = ?",
                          (account_id,))
        return {"jobs": counts, "held": sums["held"], "charged": sums["charged"],
                "mailboxes": boxes["n"]}

    def topup(self, source, account_id, credits, external_ref, note=None):
        """(topup row, created). The same (source, external_ref) credits
        once: reported again it returns the row it made, and TopUpConflict
        if the report names another account or amount."""
        with self._tx():
            found = self._one("SELECT * FROM topups WHERE source = ? AND external_ref = ?",
                              (source, external_ref))
            if found is not None:
                if (found["account_id"], found["credits"]) != (account_id, credits):
                    raise TopUpConflict("%s %s was credited to another account or amount"
                                        % (source, external_ref))
                return found, False
            topup_id = random_id()
            self._run("INSERT INTO topups (id, account_id, credits, source, external_ref, note, "
                      "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (topup_id, account_id, credits, source, external_ref, note, self.clock()))
            self._entry(account_id, credits, "topup", topup_id=topup_id, note=note)
            return self._one("SELECT * FROM topups WHERE id = ?", (topup_id,)), True

    def check_ledger(self):
        """Compare every cached balance with the sum of its ledger.

        The ledger is the record, so a balance that differs is set to the
        sum and returned as (account id, cached, ledger sum). A negative sum
        cannot be cached and is raised: the data needs a person.
        """
        fixed = []
        with self._tx():
            rows = self._all(
                "SELECT a.id, a.credits, coalesce(sum(l.delta), 0) AS total "
                "FROM accounts a LEFT JOIN ledger l ON l.account_id = a.id GROUP BY a.id")
            for row in rows:
                if row["credits"] == row["total"]:
                    continue
                if row["total"] < 0:
                    raise LedgerError("account %s: the ledger sums to %d"
                                      % (row["id"], row["total"]))
                self._run("UPDATE accounts SET credits = ? WHERE id = ?",
                          (row["total"], row["id"]))
                fixed.append((row["id"], row["credits"], row["total"]))
        for account_id, cached, total in fixed:
            log.error("account %s: balance %d did not match its ledger, set to %d",
                      account_id, cached, total)
        return fixed

    def migrate_owners(self):
        """Move rows from before accounts, owned by a key digest, to the
        account whose key has that digest, then drop the owner columns.

        Runs once accounts exist for the configured keys. A row whose digest
        no account has keeps account_id NULL: its key was already gone, and
        nothing could reach the row before either. Returns those counts.
        """
        orphans = {}
        with self._tx():
            for table in ("jobs", "mailboxes"):
                if "owner" not in self._columns(table):
                    continue
                self._run("UPDATE %s SET account_id = (SELECT id FROM accounts "
                          "WHERE key_sha256 = %s.owner) "
                          "WHERE account_id IS NULL AND owner IS NOT NULL" % (table, table))
                orphans[table] = self._one(
                    "SELECT count(*) AS n FROM %s WHERE account_id IS NULL "
                    "AND owner IS NOT NULL" % (table,))["n"]
                if table == "mailboxes":
                    self._run("DROP INDEX IF EXISTS mailboxes_owner")
                self._run("ALTER TABLE %s DROP COLUMN owner" % (table,))
        for table, count in orphans.items():
            if count:
                log.warning("%d %s owned by a key no account has were left without one",
                            count, table)
        return orphans

    # ---- access requests ---------------------------------------------------

    def add_access_request(self, name, email, what):
        with self._lock:
            cursor = self._run("INSERT INTO access_requests (name, email, what, created_at) "
                               "VALUES (?, ?, ?, ?)", (name, email, what, self.clock()))
            return self._one("SELECT * FROM access_requests WHERE id = ?",
                             (cursor.lastrowid,))

    def access_requests(self, limit, offset):
        """A page of the access requests, newest first."""
        return self._all("SELECT * FROM access_requests ORDER BY id DESC LIMIT ? OFFSET ?",
                         (limit, offset))

    # ---- in-flight ---------------------------------------------------------

    def acquire(self, contract, owner):
        """Take the one slot for contract; False if a call is in flight there."""
        key = contract.lower()
        with self._lock:
            try:
                self._run("INSERT INTO inflight (contract, owner, since) VALUES (?, ?, ?)",
                          (key, owner, self.clock()))
                return True
            except sqlite3.IntegrityError:
                row = self._one("SELECT owner FROM inflight WHERE contract = ?", (key,))
                return row is not None and row["owner"] == owner

    def holder(self, contract):
        return self._one("SELECT * FROM inflight WHERE contract = ?", (contract.lower(),))

    def set_inflight_tx(self, contract, owner, tx_id):
        self._run("UPDATE inflight SET tx_id = ? WHERE contract = ? AND owner = ?",
                  (tx_id, contract.lower(), owner))

    def release(self, contract, owner):
        self._run("DELETE FROM inflight WHERE contract = ? AND owner = ?",
                  (contract.lower(), owner))

    def release_owner(self, owner):
        self._run("DELETE FROM inflight WHERE owner = ?", (owner,))

    def inflight(self):
        return self._all("SELECT * FROM inflight ORDER BY contract")

    # ---- heartbeat ---------------------------------------------------------

    def beat(self):
        self._run("INSERT OR REPLACE INTO meta (key, value) VALUES ('heartbeat', ?)",
                  (repr(self.clock()),))

    def heartbeat(self):
        row = self._one("SELECT value FROM meta WHERE key = 'heartbeat'")
        return float(row["value"]) if row else None
