"""Hash-chained, append-only audit log with secrets redacted."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

GENESIS = "0" * 64
REDACTED = "[REDACTED]"
MAX_TEXT = 2000

_SENSITIVE_KEY = re.compile(
    r"(^|[_\-.])(pin|pins|password|passwd|secret|token|api_?key|authorization|signature)($|[_\-.])",
    re.IGNORECASE,
)


def redact(obj, secrets=()):
    """Blank values under sensitive keys and scrub known secret values anywhere."""
    if isinstance(obj, dict):
        return {
            str(k): REDACTED if _SENSITIVE_KEY.search(str(k)) else redact(v, secrets)
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact(v, secrets) for v in obj]
    if isinstance(obj, str):
        for secret in secrets:
            if secret and secret in obj:
                obj = obj.replace(secret, REDACTED)
        if len(obj) > MAX_TEXT:
            obj = obj[:MAX_TEXT] + "...[truncated]"
        return obj
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    return str(obj)


def _digest(prev_hash: str, ts: str, actor: str, event: str, detail_json: str) -> str:
    payload = json.dumps([ts, actor, event, detail_json], separators=(",", ":"))
    return hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()


@dataclass
class AuditResult:
    ok: bool
    count: int
    bad_id: int | None = None
    reason: str = ""


class AuditLog:
    def __init__(self, db, clock, secrets=()):
        self.db = db
        self.clock = clock
        self.secrets = tuple(secrets)

    def append(self, actor: str, event: str, detail=None) -> int:
        detail_json = json.dumps(
            redact(detail or {}, self.secrets), sort_keys=True, separators=(",", ":")
        )
        with self.db.transaction():
            last = self.db.one("SELECT id, hash FROM audit_log ORDER BY id DESC LIMIT 1")
            prev = last["hash"] if last else GENESIS
            ts = self.clock.now_iso()
            digest = _digest(prev, ts, actor, event, detail_json)
            cur = self.db.execute(
                "INSERT INTO audit_log (ts, actor, event, detail, prev_hash, hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts, actor, event, detail_json, prev, digest),
            )
            # Remember the head so a chopped-off tail is noticed too.
            self.db.kv_set("audit_head", f"{cur.lastrowid}:{digest}")
            return cur.lastrowid

    def verify(self) -> AuditResult:
        with self.db.lock:
            rows = self.db.query("SELECT * FROM audit_log ORDER BY id")
            prev = GENESIS
            expected_id = None
            for row in rows:
                if expected_id is not None and row["id"] != expected_id:
                    return AuditResult(False, len(rows), row["id"], "a row is missing")
                if row["prev_hash"] != prev:
                    return AuditResult(False, len(rows), row["id"], "chain link broken")
                digest = _digest(prev, row["ts"], row["actor"], row["event"], row["detail"])
                if digest != row["hash"]:
                    return AuditResult(False, len(rows), row["id"], "row was changed")
                prev = row["hash"]
                expected_id = row["id"] + 1
            head = self.db.kv_get("audit_head")
            if rows:
                want = f"{rows[-1]['id']}:{rows[-1]['hash']}"
                if head != want:
                    return AuditResult(False, len(rows), rows[-1]["id"], "end of log is missing")
            elif head:
                return AuditResult(False, 0, None, "log was emptied")
            return AuditResult(True, len(rows))

    def recent(self, limit: int = 30) -> list:
        rows = self.db.query(
            "SELECT id, ts, actor, event, detail FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "actor": r["actor"],
                "event": r["event"],
                "detail": json.loads(r["detail"]),
            }
            for r in rows
        ]

    def since(self, ts_iso: str, event: str | None = None) -> list:
        sql = "SELECT id, ts, actor, event, detail FROM audit_log WHERE ts >= ?"
        params = [ts_iso]
        if event:
            sql += " AND event = ?"
            params.append(event)
        rows = self.db.query(sql + " ORDER BY id", params)
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "actor": r["actor"],
                "event": r["event"],
                "detail": json.loads(r["detail"]),
            }
            for r in rows
        ]
