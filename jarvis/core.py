"""Jarvis: the single gate every action passes through."""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import timedelta

from .approvals import ApprovalError, Approvals, canonical, params_hash
from .audit import AuditLog
from .clock import Clock, in_send_window, iso
from .db import Database
from .pins import PinStore
from .policy import Facts, PolicyEngine, Tier


@dataclass
class Result:
    status: str  # done | pending | refused | error
    action: str
    tier: int
    value: object = None
    approval_id: str | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "done"


class Jarvis:
    def __init__(self, settings, db=None, clock=None):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock or Clock()
        self.db = db or Database(settings.db_path)
        self.audit = AuditLog(self.db, self.clock, settings.secrets())
        self.pins = PinStore(
            self.db, self.clock, self.audit, settings.get_int("PIN_ITERATIONS", 200_000)
        )
        self.approvals = Approvals(
            self.db, self.clock, self.pins, self.audit,
            settings.get_int("APPROVAL_TTL_MINUTES", 720),
        )
        self.policy = PolicyEngine()
        self.recipes = {}
        self.summaries = {}
        self._register_core()

    # --- recipes: how an approved action runs itself -----------------------

    def register(self, action: str, handler, summary=None) -> None:
        """Register the recipe for an action, and how to describe it to the owner."""
        self.recipes[action] = handler
        if summary is not None:
            self.summaries[action] = summary

    def _register_core(self) -> None:
        self.register("system.stop", self._do_stop, lambda p: "Stop everything")
        self.register(
            "system.unstop", self._do_unstop,
            lambda p: "Lift the hard stop and let Jarvis act again",
        )

    def _summarise(self, action: str, params: dict) -> str:
        maker = self.summaries.get(action)
        if maker is not None:
            try:
                return str(maker(params))[:300]
            except Exception:
                pass
        return f"{action} {canonical(params)}"[:300]

    # --- state ------------------------------------------------------------

    def is_stopped(self) -> bool:
        return self.db.kv_get("stopped") == "1" or self.settings.stop_file.exists()

    def is_live(self) -> bool:
        return self.db.kv_get("live") == "1"

    def cap(self) -> int:
        return int(self.db.kv_get("daily_cap", "0"))

    def sends_today(self, kind: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM send_log WHERE day = ?"
        params = [self.clock.local_day()]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        return self.db.scalar(sql, params)

    def remaining_today(self) -> int:
        return max(0, self.cap() - self.sends_today())

    def _is_suppressed(self, email: str) -> bool:
        return (
            self.db.one(
                "SELECT 1 FROM suppression WHERE email = ?", (str(email).strip().lower(),)
            )
            is not None
        )

    def facts(self) -> Facts:
        now = self.clock.now()
        minute_ago = iso(now - timedelta(seconds=60))
        approved = {
            r["name"]: r["body_hash"]
            for r in self.db.query("SELECT name, body_hash FROM templates WHERE approved = 1")
        }
        return Facts(
            stopped=self.is_stopped(),
            live=self.is_live(),
            cap=self.cap(),
            max_daily_cap=self.settings.get_int("MAX_DAILY_CAP", 50),
            sends_today=self.sends_today(),
            followups_today=self.sends_today("followup"),
            sends_last_minute=self.db.scalar(
                "SELECT COUNT(*) FROM send_log WHERE ts > ?", (minute_ago,)
            ),
            in_window=in_send_window(now),
            approved_templates=approved,
            is_suppressed=self._is_suppressed,
            calls_today=self.db.scalar(
                "SELECT COUNT(*) FROM calls WHERE day = ?", (self.clock.local_day(),)
            ),
            max_calls_per_day=self.settings.get_int("MAX_CALLS_PER_DAY", 6),
        )

    # --- the gate ---------------------------------------------------------

    def execute(self, action, params=None, handler=None, actor: str = "jarvis") -> Result:
        """Run an action if, and only if, the policy engine allows it."""
        params = {} if params is None else params
        try:
            if not isinstance(params, dict):
                raise TypeError("params must be a dict")
            params = json.loads(canonical(params))
        except (TypeError, ValueError) as exc:
            self.audit.append(actor, "action.refused", {"action": str(action), "reason": str(exc)})
            return Result("refused", str(action), int(Tier.T3), reason=f"bad params: {exc}")

        # SQLite's writer lock also serializes separate App instances/processes.
        # Hold it from the cap check through the send ledger update.
        transaction = self.db.transaction() if action == "email.send" else nullcontext()
        with self.db.lock, transaction:
            decision = self.policy.decide(action, params, self.facts())
            tier = int(decision.tier)
            base = {"action": decision.action, "tier": tier, "params": params}

            if not decision.allowed:
                self.audit.append(actor, "action.refused", {**base, "reason": decision.reason})
                return Result("refused", decision.action, tier, reason=decision.reason)

            if decision.needs_approval and not self.approvals.consume(action, params):
                approval_id = self.approvals.request(
                    action, params, self._summarise(action, params), actor
                )
                self.audit.append(actor, "action.pending", {**base, "approval": approval_id})
                return Result(
                    "pending", action, tier, approval_id=approval_id,
                    reason="waiting for the owner's PIN approval",
                )

            handler = handler or self.recipes.get(action)
            if handler is None:
                self.audit.append(actor, "action.error", {**base, "reason": "no handler"})
                return Result("error", action, tier, reason="no handler for this action")

            try:
                value = handler(params)
            except Exception as exc:  # the gate must never crash its caller
                self.audit.append(
                    actor, "action.error",
                    {**base, "reason": f"{type(exc).__name__}: {exc}"},
                )
                return Result("error", action, tier, reason=f"{type(exc).__name__}: {exc}")

            if decision.counts_as_send:
                self.db.execute(
                    "INSERT INTO send_log (day, kind, ref, ts) VALUES (?, ?, ?, ?)",
                    (
                        self.clock.local_day(),
                        str(params.get("kind", "initial")),
                        str(params.get("outreach_id", "")),
                        self.clock.now_iso(),
                    ),
                )
            self.audit.append(actor, "action.done", {**base, "value": _brief_value(value)})
            return Result("done", action, tier, value=value)

    # --- approvals --------------------------------------------------------

    def approve(self, approval_id: str, pin: str, purpose: str = "approval",
                via: str = "cli") -> Result:
        """Owner approves with a PIN; the approved action then runs itself."""
        self.approvals.approve(approval_id, pin, purpose=purpose, via=via)
        return self.resume(approval_id)

    def resume(self, approval_id: str) -> Result:
        """Run an approved request through the gate using its registered recipe."""
        row = self.approvals.get(approval_id)
        if row is None:
            raise ApprovalError("no such approval")
        if row["status"] != "approved":
            raise ApprovalError(f"approval is {row['status']}, not approved")
        params = json.loads(row["params_json"])
        if params_hash(row["action"], params) != row["params_hash"]:
            self.audit.append("system", "approval.tampered", {"id": approval_id})
            raise ApprovalError("stored parameters do not match their hash")
        if row["action"] not in self.recipes:
            raise ApprovalError(f"no recipe registered for {row['action']}")
        return self.execute(row["action"], params, actor="owner-approved")

    # --- hard stop --------------------------------------------------------

    def stop(self, reason: str, actor: str = "owner") -> Result:
        return self.execute("system.stop", {"reason": reason}, actor=actor)

    def _do_stop(self, params):
        self.db.kv_set("stopped", "1")
        self.db.kv_set("stop_reason", str(params.get("reason", ""))[:200])
        return "stopped"

    def _do_unstop(self, params):
        self.db.kv_set("stopped", "0")
        if self.settings.stop_file.exists():
            self.settings.stop_file.unlink()
        return "running"

    # --- brief ------------------------------------------------------------

    def brief_items(self, since_iso: str) -> list:
        """T1 actions done since a time: what ran on its own that the owner should know."""
        return [
            e for e in self.audit.since(since_iso, "action.done")
            if e["detail"].get("tier") == int(Tier.T1)
        ]


def _brief_value(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value)
            return value if len(text) <= 500 else text[:500] + "..."
        except (TypeError, ValueError):
            pass
    return str(value)[:500]
