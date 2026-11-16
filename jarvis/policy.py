"""The policy engine. It alone decides whether an action may run.

Tiers:
  T0  runs automatically
  T1  runs automatically and is listed in the owner's brief
  T2  needs a PIN approval bound to the exact parameters
  T3  never runs
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable

FOLLOWUP_SHARE = 0.30
MAX_SENDS_PER_MINUTE = 3


class Tier(IntEnum):
    T0 = 0
    T1 = 1
    T2 = 2
    T3 = 3


RULES = {
    # T0: reading, and writing our own notes
    "status.read": Tier.T0,
    "crm.read": Tier.T0,
    "lead.import": Tier.T0,
    "lead.verify": Tier.T0,
    "outreach.draft": Tier.T0,
    "outreach.queue": Tier.T0,
    "outreach.cancel": Tier.T0,
    "reply.record": Tier.T0,
    "summary.build": Tier.T0,
    "template.propose": Tier.T0,
    # T1: has an effect, runs on its own, the owner is told
    "lead.promote": Tier.T1,
    "email.send": Tier.T1,
    "suppression.add": Tier.T1,
    "booking.record": Tier.T1,
    "call.owner_alert": Tier.T1,
    "backup.create": Tier.T1,
    "system.stop": Tier.T1,
    "sales.pause": Tier.T1,
    # T2: the owner approves with a PIN
    "template.approve": Tier.T2,
    "sales.go_live": Tier.T2,
    "cap.set": Tier.T2,
    "system.unstop": Tier.T2,
    "suppression.remove": Tier.T2,
    "payment.link.create": Tier.T2,
    # T3: never
    "pin.reveal": Tier.T3,
    "env.read": Tier.T3,
    "audit.erase": Tier.T3,
    "shell.exec": Tier.T3,
}

# Money may come in. These are the only money actions that are not T3.
MONEY_IN = {"payment.link.create", "payment.record_received"}
RULES.setdefault("payment.record_received", Tier.T1)

_MONEY_TOKENS = {
    "pay", "pays", "payout", "payouts", "payment", "payments", "refund", "refunds",
    "transfer", "transfers", "withdraw", "withdrawal", "wire", "remit", "disburse",
    "purchase", "buy", "spend", "tip", "donate", "bank", "money",
}
_OUT_DIRECTIONS = {"out", "outgoing", "outbound", "debit", "send"}

# What may still run while the hard stop is on (besides T0).
ALLOWED_WHEN_STOPPED = {
    "system.stop",
    "system.unstop",
    "call.owner_alert",
    "backup.create",
    "suppression.add",
    "booking.record",
}


def is_money_out(action: str, params: dict) -> bool:
    direction = str(params.get("direction", "")).lower()
    if direction in _OUT_DIRECTIONS:
        return True
    if action in MONEY_IN:
        return False
    tokens = set(re.split(r"[^a-z]+", action.lower()))
    return bool(tokens & _MONEY_TOKENS)


@dataclass
class Facts:
    """The state of the world the engine judges against. Built by Jarvis."""

    stopped: bool = False
    live: bool = False
    cap: int = 0
    max_daily_cap: int = 50
    sends_today: int = 0
    followups_today: int = 0
    sends_last_minute: int = 0
    in_window: bool = False
    approved_templates: dict = field(default_factory=dict)
    is_suppressed: Callable = lambda email: False
    calls_today: int = 0
    max_calls_per_day: int = 6


@dataclass(frozen=True)
class Decision:
    action: str
    tier: Tier
    allowed: bool
    needs_approval: bool = False
    reason: str = ""
    counts_as_send: bool = False


def followup_limit(cap: int) -> int:
    return int(cap * FOLLOWUP_SHARE)


class PolicyEngine:
    def decide(self, action, params: dict, facts: Facts) -> Decision:
        if not isinstance(action, str) or not action:
            return Decision(str(action), Tier.T3, False, reason="no action named")
        if is_money_out(action, params):
            return Decision(action, Tier.T3, False, reason="money out is never allowed")
        tier = RULES.get(action)
        if tier is None:
            return Decision(action, Tier.T3, False, reason="unknown action")
        if tier == Tier.T3:
            return Decision(action, tier, False, reason="this action is never allowed")
        if facts.stopped and tier != Tier.T0 and action not in ALLOWED_WHEN_STOPPED:
            return Decision(action, tier, False, reason="hard stop is on")

        guard = getattr(self, "_guard_" + action.replace(".", "_"), None)
        if guard is not None:
            problem = guard(params, facts)
            if problem:
                return Decision(action, tier, False, reason=problem)

        return Decision(
            action,
            tier,
            True,
            needs_approval=(tier == Tier.T2),
            counts_as_send=(action == "email.send"),
        )

    # Guards return a reason to refuse, or None to allow.

    def _guard_email_send(self, params, facts):
        to = str(params.get("to", ""))
        if not to:
            return "no recipient"
        if not facts.live:
            return "not live: go-live has not been approved"
        if facts.cap <= 0:
            return "no daily cap has been approved"
        template = params.get("template")
        approved_hash = facts.approved_templates.get(template)
        if not approved_hash or approved_hash != params.get("template_hash"):
            return "this wording has not been approved"
        if facts.is_suppressed(to):
            return "recipient is suppressed"
        if not facts.in_window:
            return "outside the sending window (weekdays 09:30-16:30)"
        if facts.sends_today >= facts.cap:
            return "daily cap reached"
        if facts.sends_last_minute >= MAX_SENDS_PER_MINUTE:
            return "rate limit: at most 3 a minute"
        if params.get("kind") == "followup":
            if facts.followups_today >= followup_limit(facts.cap):
                return "follow-up share of the cap reached"
        return None

    def _guard_cap_set(self, params, facts):
        cap = params.get("cap")
        if not isinstance(cap, int) or isinstance(cap, bool):
            return "cap must be a whole number"
        if cap < 1 or cap > facts.max_daily_cap:
            return f"cap must be between 1 and {facts.max_daily_cap}"
        return None

    def _guard_call_owner_alert(self, params, facts):
        if facts.calls_today >= facts.max_calls_per_day:
            return "daily limit on owner calls reached"
        return None
