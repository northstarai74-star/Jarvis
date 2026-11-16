"""The CRM data layer: firms, shops, outreach, replies, suppression.

These are plain reads and writes. Anything with an effect is called from a
handler that already went through Jarvis.execute.
"""

from __future__ import annotations

import re

_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def normalise_email(email) -> str:
    return str(email or "").strip().lower()


def valid_email(email) -> bool:
    email = str(email or "")
    return len(email) <= 254 and bool(_EMAIL.match(email))


def clean_text(value, limit: int = 200) -> str:
    """One tidy line of text: no control characters, no line breaks, capped."""
    text = _CONTROL.sub(" ", str(value or ""))
    return " ".join(text.split())[:limit]


class Crm:
    def __init__(self, db, clock):
        self.db = db
        self.clock = clock

    # firms and shops

    def add_firm(self, name, email, company_number="", email_source="", town="",
                 lead_id=None) -> int:
        email = normalise_email(email)
        if not valid_email(email):
            raise ValueError("not a valid email address")
        cur = self.db.execute(
            "INSERT INTO firms (name, company_number, email, email_source, town, lead_id, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (clean_text(name), clean_text(company_number, 20), email,
             clean_text(email_source, 500), clean_text(town, 80), lead_id,
             self.clock.now_iso()),
        )
        return cur.lastrowid

    def firm(self, firm_id):
        return self.db.one("SELECT * FROM firms WHERE id = ?", (firm_id,))

    def firm_by_email(self, email):
        return self.db.one("SELECT * FROM firms WHERE email = ?", (normalise_email(email),))

    def firms(self, status=None) -> list:
        if status:
            return self.db.query("SELECT * FROM firms WHERE status = ? ORDER BY id", (status,))
        return self.db.query("SELECT * FROM firms ORDER BY id")

    def set_firm_status(self, firm_id, status: str) -> None:
        self.db.execute("UPDATE firms SET status = ? WHERE id = ?", (status, firm_id))

    def add_shop(self, firm_id, name, address="", postcode="", phone="") -> int:
        cur = self.db.execute(
            "INSERT INTO shops (firm_id, name, address, postcode, phone) VALUES (?, ?, ?, ?, ?)",
            (firm_id, clean_text(name), clean_text(address, 300), clean_text(postcode, 12),
             clean_text(phone, 30)),
        )
        return cur.lastrowid

    def shops(self, firm_id) -> list:
        return self.db.query("SELECT * FROM shops WHERE firm_id = ? ORDER BY id", (firm_id,))

    # outreach

    def add_outreach(self, firm_id, kind, template, template_hash, subject, body) -> int:
        cur = self.db.execute(
            "INSERT INTO outreach (firm_id, kind, template, template_hash, subject, body, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)",
            (firm_id, kind, template, template_hash, subject, body, self.clock.now_iso()),
        )
        return cur.lastrowid

    def outreach(self, outreach_id):
        return self.db.one("SELECT * FROM outreach WHERE id = ?", (outreach_id,))

    def outreach_for(self, firm_id) -> list:
        return self.db.query("SELECT * FROM outreach WHERE firm_id = ? ORDER BY id", (firm_id,))

    def live_outreach(self, firm_id, kind):
        """The firm's outreach of this kind that is not cancelled or failed."""
        return self.db.one(
            "SELECT * FROM outreach WHERE firm_id = ? AND kind = ? "
            "AND status IN ('draft', 'queued', 'sent')",
            (firm_id, kind),
        )

    def cancel_unsent(self, firm_id) -> int:
        cur = self.db.execute(
            "UPDATE outreach SET status = 'cancelled' "
            "WHERE firm_id = ? AND status IN ('draft', 'queued')",
            (firm_id,),
        )
        return cur.rowcount

    # replies

    def add_reply(self, firm_id, from_email, subject, body, classification) -> int:
        cur = self.db.execute(
            "INSERT INTO replies (firm_id, from_email, subject, body, classification, "
            "received_at) VALUES (?, ?, ?, ?, ?, ?)",
            (firm_id, normalise_email(from_email), clean_text(subject, 300),
             str(body)[:20000], classification, self.clock.now_iso()),
        )
        return cur.lastrowid

    def replies(self, firm_id=None) -> list:
        if firm_id is None:
            return self.db.query("SELECT * FROM replies ORDER BY id")
        return self.db.query("SELECT * FROM replies WHERE firm_id = ? ORDER BY id", (firm_id,))

    # suppression

    def suppress(self, email, reason: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO suppression (email, reason, created_at) VALUES (?, ?, ?)",
            (normalise_email(email), clean_text(reason, 100), self.clock.now_iso()),
        )

    def unsuppress(self, email) -> None:
        self.db.execute("DELETE FROM suppression WHERE email = ?", (normalise_email(email),))

    def is_suppressed(self, email) -> bool:
        return (
            self.db.one("SELECT 1 FROM suppression WHERE email = ?", (normalise_email(email),))
            is not None
        )
