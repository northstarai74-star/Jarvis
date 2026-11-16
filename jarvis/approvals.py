"""PIN approvals, each bound to a hash of the exact action and parameters.

An approval is single use and expires. Changing any parameter changes the
hash, so an approval for one thing can never be spent on another.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta

from .clock import iso


class ApprovalError(Exception):
    pass


def canonical(params: dict) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def params_hash(action: str, params: dict) -> str:
    text = canonical({"action": action, "params": params})
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Approvals:
    def __init__(self, db, clock, pins, audit, ttl_minutes: int = 720):
        self.db = db
        self.clock = clock
        self.pins = pins
        self.audit = audit
        self.ttl_minutes = ttl_minutes

    def _expire_stale(self) -> None:
        self.db.execute(
            "UPDATE approvals SET status = 'expired' "
            "WHERE status IN ('pending', 'approved') AND expires_at <= ?",
            (self.clock.now_iso(),),
        )

    def request(self, action: str, params: dict, summary: str, actor: str) -> str:
        """Ask for approval. Asking again for the same thing returns the same request."""
        digest = params_hash(action, params)
        with self.db.transaction():
            self._expire_stale()
            existing = self.db.one(
                "SELECT id FROM approvals WHERE action = ? AND params_hash = ? "
                "AND status = 'pending'",
                (action, digest),
            )
            if existing:
                return existing["id"]
            approval_id = secrets.token_hex(4)
            now = self.clock.now()
            self.db.execute(
                "INSERT INTO approvals (id, action, params_hash, params_json, summary, "
                "status, actor, created_at, expires_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    approval_id,
                    action,
                    digest,
                    canonical(params),
                    summary,
                    actor,
                    iso(now),
                    iso(now + timedelta(minutes=self.ttl_minutes)),
                ),
            )
            return approval_id

    def get(self, approval_id: str):
        self._expire_stale()
        return self.db.one("SELECT * FROM approvals WHERE id = ?", (approval_id,))

    def pending(self) -> list:
        self._expire_stale()
        return self.pending_readonly()

    def pending_readonly(self) -> list:
        """Read active requests without expiring stored rows (dashboard GET)."""
        return self.db.query(
            "SELECT * FROM approvals WHERE status = 'pending' AND expires_at > ? "
            "ORDER BY created_at, rowid", (self.clock.now_iso(),)
        )

    def _require_pending(self, approval_id: str):
        row = self.get(approval_id)
        if row is None:
            raise ApprovalError("no such approval")
        if row["status"] != "pending":
            raise ApprovalError(f"approval is {row['status']}, not pending")
        return row

    def approve(self, approval_id: str, pin: str, purpose: str = "approval", via: str = "cli"):
        """Check the PIN, then mark the request approved. Raises on any problem."""
        self._require_pending(approval_id)
        if not self.pins.verify(purpose, pin):
            raise ApprovalError("wrong PIN")
        return self.mark_approved(approval_id, via)

    def mark_approved(self, approval_id: str, via: str):
        """Approve after the PIN was already checked (for example earlier in a call)."""
        with self.db.transaction():
            row = self._require_pending(approval_id)
            self.db.execute(
                "UPDATE approvals SET status = 'approved', decided_at = ?, decided_via = ? "
                "WHERE id = ? AND status = 'pending'",
                (self.clock.now_iso(), via, approval_id),
            )
            self.audit.append(
                "owner", "approval.approved",
                {"id": approval_id, "action": row["action"], "via": via},
            )
            return self.get(approval_id)

    def reject(self, approval_id: str, via: str = "cli") -> None:
        with self.db.transaction():
            row = self._require_pending(approval_id)
            self.db.execute(
                "UPDATE approvals SET status = 'rejected', decided_at = ?, decided_via = ? "
                "WHERE id = ?",
                (self.clock.now_iso(), via, approval_id),
            )
            self.audit.append(
                "owner", "approval.rejected",
                {"id": approval_id, "action": row["action"], "via": via},
            )

    def consume(self, action: str, params: dict) -> bool:
        """Spend an approved request for exactly this action and these parameters."""
        digest = params_hash(action, params)
        with self.db.transaction():
            self._expire_stale()
            row = self.db.one(
                "SELECT id FROM approvals WHERE action = ? AND params_hash = ? "
                "AND status = 'approved' ORDER BY decided_at LIMIT 1",
                (action, digest),
            )
            if row is None:
                return False
            cur = self.db.execute(
                "UPDATE approvals SET status = 'consumed' WHERE id = ? AND status = 'approved'",
                (row["id"],),
            )
            return cur.rowcount == 1
