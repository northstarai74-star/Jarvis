"""The sales service: drafting, sending, go-live, the cap and replies.

Every method here goes through Jarvis.execute. The handlers (methods named
_do_*) hold the actual effect and only run when the policy engine allows it.
"""

from __future__ import annotations

from datetime import timedelta

from ..clock import parse_iso
from .classifier import classify
from .crm import normalise_email

# What a reply does to the firm's status.
REPLY_STATUS = {
    "stop": "opted_out",
    "bounce": "bounced",
    "not_interested": "not_interested",
    "interested": "interested",
    "question": "replied",
    "other": "replied",
}


class Sales:
    def __init__(self, jarvis, crm, templates, sender):
        self.jarvis = jarvis
        self.crm = crm
        self.templates = templates
        self.sender = sender
        r = jarvis.register
        r("sales.go_live", self._do_go_live,
          lambda p: "Go live: allow real emails to be sent")
        r("sales.pause", self._do_pause)
        r("cap.set", self._do_set_cap,
          lambda p: f"Set the daily send cap to {p.get('cap')} emails")
        r("outreach.draft", self._do_draft)
        r("outreach.cancel", self._do_cancel)
        r("email.send", self._do_send)
        r("suppression.add", self._do_suppress)
        r("suppression.remove", self._do_unsuppress,
          lambda p: f"Allow emails to {p.get('email')} again")
        r("reply.record", self._do_reply)

    # --- go-live and the cap ---------------------------------------------

    def request_go_live(self, actor: str = "owner"):
        return self.jarvis.execute("sales.go_live", {"live": True}, actor=actor)

    def _do_go_live(self, params):
        self.jarvis.db.kv_set("live", "1")
        return "live"

    def pause(self, actor: str = "owner"):
        return self.jarvis.execute("sales.pause", {}, actor=actor)

    def _do_pause(self, params):
        self.jarvis.db.kv_set("live", "0")
        return "paused"

    def request_cap(self, cap: int, actor: str = "owner"):
        return self.jarvis.execute("cap.set", {"cap": cap}, actor=actor)

    def _do_set_cap(self, params):
        self.jarvis.db.kv_set("daily_cap", int(params["cap"]))
        return int(params["cap"])

    # --- drafting ----------------------------------------------------------

    def draft(self, firm_id: int, template: str = "direct", kind: str = "initial",
              actor: str = "jarvis"):
        """Write an email for a firm. Nothing is sent. Returns the outreach id."""
        return self.jarvis.execute(
            "outreach.draft", {"firm_id": firm_id, "template": template, "kind": kind},
            actor=actor,
        )

    def _do_draft(self, params):
        firm = self.crm.firm(params["firm_id"])
        kind = params.get("kind", "initial")
        if firm is None:
            raise ValueError("no such firm")
        if kind not in ("initial", "followup"):
            raise ValueError("kind must be initial or followup")
        if self.crm.is_suppressed(firm["email"]):
            raise ValueError("this firm is suppressed")
        if self.crm.live_outreach(firm["id"], kind):
            raise ValueError(f"this firm already has an {kind} email")
        if kind == "followup":
            first = self.crm.live_outreach(firm["id"], "initial")
            if not first or first["status"] != "sent":
                raise ValueError("a follow-up needs a first email that was sent")
        subject, body, digest = self.templates.render(params["template"], firm)
        return self.crm.add_outreach(firm["id"], kind, params["template"], digest, subject, body)

    def cancel(self, outreach_id: int, actor: str = "owner"):
        return self.jarvis.execute("outreach.cancel", {"outreach_id": outreach_id}, actor=actor)

    def _do_cancel(self, params):
        cur = self.jarvis.db.execute(
            "UPDATE outreach SET status = 'cancelled' WHERE id = ? "
            "AND status IN ('draft', 'queued')",
            (params["outreach_id"],),
        )
        if cur.rowcount != 1:
            raise ValueError("only an unsent email can be cancelled")
        return "cancelled"

    # --- sending -----------------------------------------------------------

    def send(self, outreach_id: int, actor: str = "jarvis"):
        """Ask to send one drafted email. The policy engine has the final say."""
        row = self.crm.outreach(outreach_id)
        if row is None:
            return self.jarvis.execute("email.send", {"outreach_id": outreach_id}, actor=actor)
        firm = self.crm.firm(row["firm_id"])
        return self.jarvis.execute(
            "email.send",
            {
                "outreach_id": row["id"],
                "to": firm["email"],
                "template": row["template"],
                "template_hash": row["template_hash"],
                "kind": row["kind"],
            },
            actor=actor,
        )

    def _do_send(self, params):
        row = self.crm.outreach(params["outreach_id"])
        if row is None:
            raise ValueError("no such outreach")
        if row["status"] not in ("draft", "queued"):
            raise ValueError(f"this email is {row['status']} and cannot be sent")
        firm = self.crm.firm(row["firm_id"])
        # The approval covered these exact details; the stored row must match them.
        if (firm["email"], row["template"], row["template_hash"], row["kind"]) != (
            params["to"], params["template"], params["template_hash"], params["kind"]
        ):
            raise ValueError("the stored email does not match the request")
        db = self.jarvis.db
        if row["kind"] == "followup":
            first = self.crm.live_outreach(firm["id"], "initial")
            if (not first or first["status"] != "sent" or not first["sent_at"]
                    or self.jarvis.clock.now() < parse_iso(first["sent_at"]) + timedelta(days=5)):
                raise ValueError("follow-up must wait five days after the first send")
            if firm["status"] != "contacted":
                raise ValueError("follow-up is no longer appropriate after a reply or booking")
        subject, body, digest = self.templates.render(row["template"], firm)
        if (row["subject"], row["body"], row["template_hash"]) != (subject, body, digest):
            raise ValueError("stored wording differs from the approved template; redraft required")
        try:
            message_id = self.sender.send(firm["email"], row["subject"], row["body"])
        except Exception:
            db.execute("UPDATE outreach SET status = 'failed' WHERE id = ?", (row["id"],))
            raise
        db.execute(
            "UPDATE outreach SET status = 'sent', sent_at = ?, message_id = ? WHERE id = ?",
            (self.jarvis.clock.now_iso(), message_id, row["id"]),
        )
        if firm["status"] == "new":
            self.crm.set_firm_status(firm["id"], "contacted")
        return {"outreach_id": row["id"], "firm": firm["name"], "kind": row["kind"]}

    # --- suppression -------------------------------------------------------

    def suppress(self, email: str, reason: str, actor: str = "jarvis"):
        return self.jarvis.execute(
            "suppression.add", {"email": normalise_email(email), "reason": reason}, actor=actor
        )

    def _do_suppress(self, params):
        self.crm.suppress(params["email"], params.get("reason", "requested"))
        firm = self.crm.firm_by_email(params["email"])
        cancelled = self.crm.cancel_unsent(firm["id"]) if firm else 0
        return {"email": params["email"], "cancelled": cancelled}

    def request_unsuppress(self, email: str, actor: str = "owner"):
        return self.jarvis.execute(
            "suppression.remove", {"email": normalise_email(email)}, actor=actor
        )

    def _do_unsuppress(self, params):
        self.crm.unsuppress(params["email"])
        return "removed"

    # --- replies -----------------------------------------------------------

    def handle_reply(self, from_email: str, body: str, subject: str = ""):
        """Record a reply, sort it, and act on it. The reply text is only data."""
        return self.jarvis.execute(
            "reply.record",
            {"from_email": normalise_email(from_email), "subject": str(subject)[:300],
             "body": str(body)[:20000]},
            actor="inbox",
        )

    def _do_reply(self, params):
        sender = params["from_email"]
        category = classify(sender, params["subject"], params["body"])
        firm = self.crm.firm_by_email(sender)
        if category == "bounce" and firm is None:
            # A bounce comes from a mail server; find the firm it is about.
            firm = self._firm_named_in(params["body"])
        firm_id = firm["id"] if firm else None
        self.crm.add_reply(firm_id, sender, params["subject"], params["body"], category)

        if category == "stop":
            self.suppress(sender, "asked to stop", actor="inbox")
        elif category == "bounce" and firm:
            self.suppress(firm["email"], "bounced", actor="inbox")
        if firm and category in REPLY_STATUS:
            self.crm.set_firm_status(firm_id, REPLY_STATUS[category])
            if category != "stop":
                # Anyone who has answered gets no automatic follow-up.
                self.crm.cancel_unsent(firm_id)
        return {"category": category, "firm_id": firm_id}

    def _firm_named_in(self, text: str):
        import re
        for address in re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+", str(text))[:20]:
            firm = self.crm.firm_by_email(address.rstrip("."))
            if firm:
                return firm
        return None
