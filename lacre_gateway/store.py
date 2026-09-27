"""SQLite persistence: jobs, senders, the in-flight table and a heartbeat.

What is stored is listed column by column below, and nothing else is. No
email address, subject, header value or body is ever written here. The two
values taken from a message are the signing domain and selector (d= and s=),
because every KeyCache and Verifier call takes them as public calldata
arguments and a job may have to wait a day for its sender key; they name the
sender's mail system, not a person. bh and the blob digest are hashes.
"""

import json
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
CREATE INDEX IF NOT EXISTS jobs_open ON jobs (status) WHERE status IN ('pending', 'attesting');
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
"""

OPEN = ("pending", "attesting")
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

    def create_job(self, sender_id, headers_sha256, bh, body_hash_ok):
        now = self.clock()
        job_id = uuid.uuid4().hex
        self._run(
            "INSERT INTO jobs (id, status, stage, sender_id, headers_sha256, bh, body_hash_ok, "
            "created_at, updated_at) VALUES (?, 'pending', 'queued', ?, ?, ?, ?, ?, ?)",
            (job_id, sender_id, headers_sha256, bh,
             None if body_hash_ok is None else int(body_hash_ok), now, now))
        return job_id

    def job(self, job_id):
        row = self._one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if row:
            row["tx_ids"] = json.loads(row["tx_ids"])
            row["records_before"] = (json.loads(row["records_before"])
                                     if row["records_before"] is not None else None)
        return row

    def open_jobs(self):
        ids = self._all("SELECT id FROM jobs WHERE status IN ('pending', 'attesting') "
                        "ORDER BY created_at")
        return [self.job(r["id"]) for r in ids]

    def update_job(self, job_id, **values):
        values["updated_at"] = self.clock()
        for key in ("tx_ids", "records_before"):
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
