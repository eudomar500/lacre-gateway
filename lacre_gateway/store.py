"""SQLite persistence: jobs, senders, the in-flight table and a heartbeat.

What is stored is listed column by column below, and nothing else is. No
email address, subject, header value or body is ever written here. The two
values taken from a message are the signing domain and selector (d= and s=),
because every KeyCache and Verifier call takes them as public calldata
arguments and a job may have to wait a day for its sender key; they name the
sender's mail system, not a person. bh and the blob digest are hashes. Of an
extraction, only the fields the Extractor stored on chain are kept, and they
are public there already: booleans, a weekday, a date, digests and a fixed
reason phrase. No order number is ever among them.

A mailbox row holds its random id, the SHA-256 of the API key that owns
it (never the key), its extract mode and counters. Mail that arrives there
goes through the same path as an upload: nothing of it is written here but
what a job keeps, plus the mailbox id the job came through. The addresses
of the sender and of any other recipient are never stored. The inbound
replay table holds HMAC values only, for the length of the replay window.
"""

import base64
import json
import secrets
import sqlite3
import threading
import time
import uuid

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
CREATE TABLE IF NOT EXISTS mailboxes (
    id               TEXT PRIMARY KEY,
    owner            TEXT NOT NULL,
    extract_mode     TEXT NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1,
    received         INTEGER NOT NULL DEFAULT 0,
    dropped          INTEGER NOT NULL DEFAULT 0,
    last_received_at REAL,
    created_at       REAL NOT NULL,
    disabled_at      REAL
);
CREATE INDEX IF NOT EXISTS mailboxes_owner ON mailboxes (owner);
-- Signatures of inbound deliveries already taken, so the same signed
-- request cannot create a second job while its timestamp is still fresh.
CREATE TABLE IF NOT EXISTS inbound_seen (
    signature TEXT PRIMARY KEY,
    seen_at   REAL NOT NULL
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
# treated as "api", and has no owner.
MAILBOX_COLUMNS = (
    # api or inbound.
    ("via", "TEXT"),
    ("mailbox", "TEXT"),
    # SHA-256 of the API key the job was created for.
    ("owner", "TEXT"),
)
JSON_COLUMNS = ("tx_ids", "records_before", "ext_tx_ids", "ext_before", "ext_record")

OPEN = ("pending", "attesting", "extracting")
TERMINAL = ("finalized", "refused", "failed")


class Store:
    def __init__(self, path, clock=time.time):
        self.clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            if str(path) != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            have = {r[1] for r in self._db.execute("PRAGMA table_info(jobs)")}
            for name, kind in EXTRACTION_COLUMNS + MAILBOX_COLUMNS:
                if name not in have:
                    self._db.execute("ALTER TABLE jobs ADD COLUMN %s %s" % (name, kind))
            self._db.execute("CREATE INDEX IF NOT EXISTS jobs_mailbox ON jobs (mailbox) "
                             "WHERE mailbox IS NOT NULL")
            # v0's partial index left extracting jobs out.
            self._db.execute("DROP INDEX IF EXISTS jobs_open")
            self._db.execute("CREATE INDEX IF NOT EXISTS jobs_active ON jobs (status) "
                             "WHERE status IN ('pending', 'attesting', 'extracting')")

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
                   skipped=None, via="api", mailbox=None, owner=None):
        """A new pending job. skipped is the reason an extraction that was
        asked for cannot run, known already at upload."""
        now = self.clock()
        job_id = uuid.uuid4().hex
        self._run(
            "INSERT INTO jobs (id, status, stage, sender_id, headers_sha256, bh, body_hash_ok, "
            "extract_mode, ext_status, ext_note, via, mailbox, owner, created_at, updated_at) "
            "VALUES (?, 'pending', 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, sender_id, headers_sha256, bh,
             None if body_hash_ok is None else int(body_hash_ok), extract_mode,
             "skipped" if skipped else None, skipped, via, mailbox, owner, now, now))
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
        if "status" in values and values["status"] in TERMINAL:
            values.setdefault("finished_at", values["updated_at"])
        cols = ", ".join("%s = ?" % k for k in values)
        self._run("UPDATE jobs SET %s WHERE id = ?" % cols, (*values.values(), job_id))

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

    # ---- mailboxes -----------------------------------------------------------

    def create_mailbox(self, owner, extract_mode):
        """A new enabled mailbox with a fresh random id."""
        now = self.clock()
        for _ in range(5):
            # 8 random bytes make 13 base32 characters; the first 12 carry 60
            # uniformly random bits.
            mailbox_id = base64.b32encode(secrets.token_bytes(8)).decode("ascii")[:12].lower()
            try:
                self._run("INSERT INTO mailboxes (id, owner, extract_mode, created_at) "
                          "VALUES (?, ?, ?, ?)", (mailbox_id, owner, extract_mode, now))
            except sqlite3.IntegrityError:
                continue
            return self.mailbox(mailbox_id)
        raise RuntimeError("no free mailbox id after 5 draws")

    def mailbox(self, mailbox_id):
        return self._one("SELECT * FROM mailboxes WHERE id = ?", (mailbox_id,))

    def mailboxes(self, owner):
        return self._all("SELECT * FROM mailboxes WHERE owner = ? "
                         "ORDER BY created_at, rowid", (owner,))

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
