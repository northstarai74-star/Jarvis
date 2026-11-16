"""Autopilot: plans the day, sends on schedule, follows up, writes summaries.

It only ever asks. Each send goes through Jarvis.execute, where the policy
engine enforces the window, the cap, the rate and the follow-up share again.
"""

from __future__ import annotations

from datetime import time, timedelta

from . import ops
from .clock import (
    day_bounds_utc, in_send_window, iso, uk_local, window_end_utc, window_start_utc,
)
from .policy import MAX_SENDS_PER_MINUTE, followup_limit

FOLLOWUP_AFTER_DAYS = 5
MIN_GAP_SECONDS = 60 // MAX_SENDS_PER_MINUTE  # at most 3 a minute
PREVIEW_DRAFTS = 5
MORNING_AT = time(9, 0)
EVENING_AT = time(16, 45)


class Autopilot:
    def __init__(self, jarvis, crm, sales, leads=None):
        self.jarvis = jarvis
        self.db = jarvis.db
        self.crm = crm
        self.sales = sales
        self.leads = leads
        self.on_summary = None  # called with (kind, text) after a summary is built
        jarvis.register("outreach.queue", self._do_queue)
        jarvis.register("summary.build", self._do_summary)

    # --- choosing who to write to -----------------------------------------

    def _new_firms(self) -> list:
        return [f for f in self.crm.firms("new") if not self.crm.is_suppressed(f["email"])]

    def _followups_due(self) -> list:
        """Firms whose first email went out 5+ days ago and who never answered."""
        cutoff = iso(self.jarvis.clock.now() - timedelta(days=FOLLOWUP_AFTER_DAYS))
        rows = self.db.query(
            "SELECT f.* FROM firms f JOIN outreach o ON o.firm_id = f.id "
            "WHERE f.status = 'contacted' AND o.kind = 'initial' AND o.status = 'sent' "
            "AND o.sent_at <= ? AND NOT EXISTS (SELECT 1 FROM outreach x WHERE x.firm_id = f.id "
            "AND x.kind = 'followup' AND x.status IN ('draft', 'queued', 'sent')) "
            "ORDER BY o.sent_at, f.id",
            (cutoff,),
        )
        return [f for f in rows if not self.crm.is_suppressed(f["email"])]

    def _slot_range(self):
        """(start, end) of what is left of today's window, or None."""
        now = self.jarvis.clock.now()
        if uk_local(now).weekday() >= 5:
            return None
        end = window_end_utc(now)
        if now >= end:
            return None
        return max(now, window_start_utc(now)), end

    def _cancel_stale(self) -> int:
        """Unsent emails written from wording that has since changed are dropped."""
        cur = self.db.execute(
            "UPDATE outreach SET status = 'cancelled' WHERE status IN ('draft', 'queued') "
            "AND template_hash != (SELECT body_hash FROM templates t WHERE t.name = template)"
        )
        return cur.rowcount

    # --- the daily draft ---------------------------------------------------

    def draft_day(self, actor: str = "autopilot") -> dict:
        """Write today's emails and spread them across the rest of the window."""
        j = self.jarvis
        plan = {"first": 0, "followup": 0, "drafted": 0, "note": ""}
        if j.is_stopped():
            plan["note"] = "hard stop is on"
            return plan
        self._cancel_stale()
        approved = j.facts().approved_templates
        first_templates = [t for t in ("direct", "soft") if t in approved] or ["direct"]
        cap = j.cap()

        def first_draft(firm):
            template = first_templates[firm["id"] % len(first_templates)]
            result = self.sales.draft(firm["id"], template, "initial", actor)
            if result.ok:
                plan["drafted"] += 1
                return self.crm.outreach(result.value)
            return None

        if cap <= 0:
            for firm in self._new_firms()[:PREVIEW_DRAFTS]:
                if not self.crm.live_outreach(firm["id"], "initial"):
                    first_draft(firm)
            plan["note"] = "no daily cap approved yet: drafts are ready to read, nothing is queued"
            return plan

        slot_range = self._slot_range()
        if slot_range is None:
            plan["note"] = "no sending window left today"
            return plan
        start, end = slot_range
        by_time = int((end - start).total_seconds() // MIN_GAP_SECONDS)
        slots = min(j.remaining_today(), by_time)

        # Start again from what is already queued, then add to it.
        self.db.execute("UPDATE outreach SET scheduled_at = NULL WHERE status = 'queued'")
        chosen = list(self.db.query(
            "SELECT * FROM outreach WHERE status = 'queued' ORDER BY id LIMIT ?", (slots,)
        ))
        followups = sum(1 for row in chosen if row["kind"] == "followup")
        followup_room = followup_limit(cap) - j.sends_today("followup")

        if "followup" in approved:
            for firm in self._followups_due():
                if len(chosen) >= slots or followups >= followup_room:
                    break
                result = self.sales.draft(firm["id"], "followup", "followup", actor)
                if result.ok:
                    plan["drafted"] += 1
                    chosen.append(self.crm.outreach(result.value))
                    followups += 1

        for firm in self._new_firms():
            if len(chosen) >= slots:
                break
            existing = self.crm.live_outreach(firm["id"], "initial")
            if existing and existing["status"] != "draft":
                continue
            row = existing or first_draft(firm)
            if row is not None:
                chosen.append(row)

        if chosen:
            gap = max(timedelta(seconds=MIN_GAP_SECONDS), (end - start) / len(chosen))
            for i, row in enumerate(chosen):
                j.execute(
                    "outreach.queue",
                    {"outreach_id": row["id"], "scheduled_at": iso(start + gap * i)},
                    actor=actor,
                )
                plan["followup" if row["kind"] == "followup" else "first"] += 1
        if not plan["note"] and not chosen:
            plan["note"] = "nothing to send today"
        return plan

    def _do_queue(self, params):
        cur = self.db.execute(
            "UPDATE outreach SET status = 'queued', scheduled_at = ? "
            "WHERE id = ? AND status IN ('draft', 'queued')",
            (params["scheduled_at"], params["outreach_id"]),
        )
        if cur.rowcount != 1:
            raise ValueError("only an unsent email can be queued")
        return params["scheduled_at"]

    # --- sending -----------------------------------------------------------

    def tick(self, actor: str = "autopilot") -> dict:
        """Send what is due right now. Call this about once a minute."""
        j = self.jarvis
        ops.heartbeat(j)
        now = j.clock.now()
        out = {"sent": 0, "note": ""}
        if not in_send_window(now):
            out["note"] = "outside the sending window"
            return out
        due = self.db.query(
            "SELECT id FROM outreach WHERE status = 'queued' AND scheduled_at IS NOT NULL "
            "AND scheduled_at >= ? AND scheduled_at <= ? ORDER BY scheduled_at, id LIMIT ?",
            (iso(window_start_utc(now)), iso(now), MAX_SENDS_PER_MINUTE),
        )
        for row in due:
            result = self.sales.send(row["id"], actor=actor)
            if result.ok:
                out["sent"] += 1
                continue
            out["note"] = result.reason
            if result.status == "refused" and "suppressed" in result.reason:
                self.sales.cancel(row["id"], actor=actor)
                continue
            break
        return out

    # --- summaries ---------------------------------------------------------

    def morning_summary(self):
        return self.jarvis.execute("summary.build", {"kind": "morning"}, actor="autopilot")

    def evening_summary(self):
        return self.jarvis.execute("summary.build", {"kind": "evening"}, actor="autopilot")

    def _do_summary(self, params):
        kind = params.get("kind")
        if kind not in ("morning", "evening"):
            raise ValueError("kind must be morning or evening")
        text = self._morning_text() if kind == "morning" else self._evening_text()
        self.db.kv_set(f"summary_{kind}", text)
        self.db.kv_set(f"summary_{kind}_at", self.jarvis.clock.now_iso())
        if self.on_summary is not None:
            self.on_summary(kind, text)
        return text

    def _pending_lines(self) -> list:
        pending = self.jarvis.approvals.pending()
        if not pending:
            return ["Nothing is waiting for your approval."]
        lines = [f"Waiting for your approval: {len(pending)}."]
        lines += [f"  - {row['summary']} (id {row['id']})" for row in pending]
        return lines

    def _morning_text(self) -> str:
        j = self.jarvis
        local = uk_local(j.clock.now())
        queued = self.db.query("SELECT kind FROM outreach WHERE status = 'queued'")
        first = sum(1 for r in queued if r["kind"] == "initial")
        lines = [
            f"Good morning. It is {local.strftime('%A %d %B %Y')}.",
            f"Sending is {'live' if j.is_live() else 'not live'}; "
            f"the daily cap is {j.cap() or 'not set'}.",
            f"Planned today: {first} first emails and {len(queued) - first} follow-ups.",
        ]
        if j.is_stopped():
            lines.append("The hard stop is ON. Nothing will be sent.")
        if self.leads is not None:
            c = self.leads.counts()
            lines.append(
                f"Leads: {c['ready']} ready, {c['review']} to review, "
                f"{c['needs_email']} need an email."
            )
        start, end = day_bounds_utc(j.clock.now())
        meetings = self.db.query(
            "SELECT * FROM bookings WHERE status = 'booked' AND start_time >= ? "
            "AND start_time < ? ORDER BY start_time",
            (iso(start), iso(end)),
        )
        lines.append(f"Meetings today: {len(meetings)}.")
        lines += [f"  - {m['start_time']} {m['attendee_name']}" for m in meetings]
        lines += self._pending_lines()
        return "\n".join(lines)

    def _evening_text(self) -> str:
        j = self.jarvis
        start, end = (iso(t) for t in day_bounds_utc(j.clock.now()))
        sent, followups = j.sends_today(), j.sends_today("followup")
        lines = [
            f"End of day. Sent {sent} of {j.cap()} emails ({followups} follow-ups).",
        ]
        replies = self.db.query(
            "SELECT classification, COUNT(*) AS n FROM replies "
            "WHERE received_at >= ? AND received_at < ? GROUP BY classification "
            "ORDER BY classification",
            (start, end),
        )
        if replies:
            lines.append("Replies: " + ", ".join(
                f"{r['n']} {r['classification'].replace('_', ' ')}" for r in replies))
        else:
            lines.append("Replies: none.")
        booked = self.db.scalar(
            "SELECT COUNT(*) FROM bookings WHERE created_at >= ? AND created_at < ?",
            (start, end),
        )
        lines.append(f"New bookings: {booked}.")

        done = {}
        for item in j.brief_items(start):
            action = item["detail"]["action"]
            done[action] = done.get(action, 0) + 1
        if done:
            lines.append("Done without asking you: " + ", ".join(
                f"{action} x{n}" for action, n in sorted(done.items())))
        problems = len(j.audit.since(start, "action.error"))
        if problems:
            lines.append(f"Actions that failed: {problems}.")
        if j.is_stopped():
            lines.append("The hard stop is ON.")
        lines += self._pending_lines()
        return "\n".join(lines)

    # --- one call does the right thing for the time of day -----------------

    def step(self) -> dict:
        j = self.jarvis
        local = uk_local(j.clock.now())
        day = j.clock.local_day()
        did = []
        weekday = local.weekday() < 5
        if weekday and local.time() >= MORNING_AT and self.db.kv_get("autopilot_morning") != day:
            self.db.kv_set("autopilot_morning", day)
            self.draft_day()
            self.morning_summary()
            did.append("morning")
        tick = self.tick()
        if weekday and local.time() >= EVENING_AT and self.db.kv_get("autopilot_evening") != day:
            self.db.kv_set("autopilot_evening", day)
            self.evening_summary()
            did.append("evening")
        return {"did": did, "sent": tick["sent"], "note": tick["note"]}
