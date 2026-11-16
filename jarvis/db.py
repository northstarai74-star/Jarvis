"""SQLite storage. One connection, guarded by a re-entrant lock."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;

CREATE TABLE IF NOT EXISTS pins (
    purpose TEXT PRIMARY KEY,
    salt TEXT NOT NULL,
    hash TEXT NOT NULL,
    iterations INTEGER NOT NULL,
    fails INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    params_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    decided_via TEXT
);

CREATE TABLE IF NOT EXISTS send_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
    name TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 0,
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS firms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    company_number TEXT,
    email TEXT NOT NULL UNIQUE,
    email_source TEXT,
    town TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    lead_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id INTEGER NOT NULL REFERENCES firms(id),
    name TEXT NOT NULL,
    address TEXT,
    postcode TEXT,
    phone TEXT
);

CREATE TABLE IF NOT EXISTS outreach (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id INTEGER NOT NULL REFERENCES firms(id),
    kind TEXT NOT NULL,
    template TEXT NOT NULL,
    template_hash TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    scheduled_at TEXT,
    sent_at TEXT,
    message_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id INTEGER REFERENCES firms(id),
    from_email TEXT NOT NULL,
    subject TEXT,
    body TEXT NOT NULL,
    classification TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppression (
    email TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    company_number TEXT,
    company_type TEXT,
    email TEXT,
    email_source TEXT,
    website TEXT,
    town TEXT,
    phone TEXT,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sid TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    to_number TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    brief TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT '{}',
    day TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL UNIQUE,
    firm_id INTEGER REFERENCES firms(id),
    attendee_name TEXT,
    attendee_email TEXT,
    title TEXT,
    start_time TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path=":memory:"):
        self.path = str(path)
        self.lock = threading.RLock()
        self._depth = 0
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)

    def execute(self, sql: str, params=()):
        with self.lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params=()) -> list:
        with self.lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()):
        with self.lock:
            return self._conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params=()):
        row = self.one(sql, params)
        return row[0] if row else None

    @contextmanager
    def transaction(self):
        """All-or-nothing block. Safe to nest; only the outermost commits."""
        with self.lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self
            except BaseException:
                self._depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    self._conn.execute("COMMIT")

    def kv_get(self, key: str, default=None):
        value = self.scalar("SELECT value FROM kv WHERE key = ?", (key,))
        return default if value is None else value

    def kv_set(self, key: str, value) -> None:
        self.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    def backup_to(self, dest_path) -> None:
        with self.lock:
            dest = sqlite3.connect(str(dest_path))
            try:
                self._conn.backup(dest)
            finally:
                dest.close()

    def integrity_ok(self) -> bool:
        return self.scalar("PRAGMA integrity_check") == "ok"

    def close(self) -> None:
        with self.lock:
            self._conn.close()
