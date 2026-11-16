"""Email templates. Only wording the owner approved by PIN can be sent.

An approval covers the exact wording: it is bound to a hash of the subject
and body. Change one word and the template is unapproved again.
"""

from __future__ import annotations

import hashlib
import re
from string import Template

from .crm import clean_text

NAMES = ("direct", "soft", "followup")
PLACEHOLDERS = {
    "firm_name", "town", "sender_name", "business_name", "business_address", "booking_link",
}

_FOOTER = (
    "\n\nKind regards,\n$sender_name\n$business_name\n$business_address\n\n"
    "If you would rather not hear from us, reply STOP and we will not email again."
)

DEFAULTS = {
    "direct": (
        "Faster viewing bookings for $firm_name",
        "Hello,\n\n"
        "I run $business_name. We help estate and letting agents answer new enquiries "
        "within minutes and get viewings booked, including outside office hours.\n\n"
        "Would a 15-minute call be useful, to see whether it would suit $firm_name? "
        "You can pick a time here: $booking_link" + _FOOTER,
    ),
    "soft": (
        "A question about enquiries at $firm_name",
        "Hello,\n\n"
        "I am getting in touch with a few agents in $town. Many tell us that enquiries "
        "arriving in the evening or at weekends wait until the next working day for a "
        "reply, and some of those people book a viewing elsewhere in the meantime.\n\n"
        "Is that something you see at $firm_name? If it is, I would be glad to show you "
        "how we handle it. There is no obligation: $booking_link" + _FOOTER,
    ),
    "followup": (
        "Following up: viewing bookings for $firm_name",
        "Hello,\n\n"
        "I wrote last week about helping $firm_name reply to enquiries quickly and book "
        "more viewings. I will not keep writing, so this is my last note.\n\n"
        "If a short call would be useful, you can choose a time here: $booking_link" + _FOOTER,
    ),
}


def template_hash(subject: str, body: str) -> str:
    return hashlib.sha256((subject + "\n\n" + body).encode("utf-8")).hexdigest()


def check_wording(subject: str, body: str) -> None:
    """Raise ValueError if the wording breaks a rule every template must keep."""
    if not subject.strip() or not body.strip():
        raise ValueError("a template needs a subject and a body")
    if "\n" in subject or "\r" in subject:
        raise ValueError("the subject must be a single line")
    if not re.search(r"\bSTOP\b", body):
        raise ValueError("the body must tell the reader they can reply STOP")
    for text in (subject, body):
        if not Template(text).is_valid():
            raise ValueError("the template has a broken $placeholder")
        used = set(re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", text))
        unknown = used - PLACEHOLDERS
        if unknown:
            raise ValueError("unknown placeholder: " + ", ".join(sorted(unknown)))


class Templates:
    def __init__(self, jarvis):
        self.jarvis = jarvis
        self.db = jarvis.db
        for name, (subject, body) in DEFAULTS.items():
            self.db.execute(
                "INSERT OR IGNORE INTO templates (name, subject, body, body_hash) "
                "VALUES (?, ?, ?, ?)",
                (name, subject, body, template_hash(subject, body)),
            )
        jarvis.register("template.propose", self._do_propose)
        jarvis.register(
            "template.approve", self._do_approve,
            lambda p: f"Approve the wording of the '{p.get('name')}' email",
        )

    def get(self, name: str):
        return self.db.one("SELECT * FROM templates WHERE name = ?", (name,))

    def all(self) -> list:
        return self.db.query("SELECT * FROM templates ORDER BY name")

    def is_approved(self, name: str) -> bool:
        row = self.get(name)
        return bool(row and row["approved"])

    def propose(self, name: str, subject: str, body: str, actor: str = "owner"):
        """Change the wording. The new wording starts out unapproved."""
        return self.jarvis.execute(
            "template.propose", {"name": name, "subject": subject, "body": body}, actor=actor
        )

    def _do_propose(self, params):
        name, subject, body = params["name"], params["subject"], params["body"]
        if name not in NAMES:
            raise ValueError(f"unknown template: {name}")
        check_wording(subject, body)
        self.db.execute(
            "UPDATE templates SET subject = ?, body = ?, body_hash = ?, approved = 0, "
            "approved_at = NULL WHERE name = ?",
            (subject, body, template_hash(subject, body), name),
        )
        return {"name": name, "approved": False}

    def request_approval(self, name: str, actor: str = "owner"):
        """Ask the owner to approve the current wording (T2, needs the PIN)."""
        row = self.get(name)
        if row is None:
            raise ValueError(f"unknown template: {name}")
        return self.jarvis.execute(
            "template.approve", {"name": name, "hash": row["body_hash"]}, actor=actor
        )

    def _do_approve(self, params):
        row = self.get(params.get("name"))
        if row is None:
            raise ValueError("unknown template")
        if row["body_hash"] != params.get("hash"):
            raise ValueError("the wording changed after approval was requested")
        self.db.execute(
            "UPDATE templates SET approved = 1, approved_at = ? WHERE name = ?",
            (self.jarvis.clock.now_iso(), row["name"]),
        )
        return {"name": row["name"], "approved": True}

    def render(self, name: str, firm) -> tuple:
        """Fill a template for a firm. Returns (subject, body, template_hash).

        Firm details are data: they are cleaned and dropped into the text as
        plain values. Nothing in them is ever treated as a template or command.
        """
        row = self.get(name)
        if row is None:
            raise ValueError(f"unknown template: {name}")
        s = self.jarvis.settings
        values = {
            "firm_name": clean_text(firm["name"], 120) or "your agency",
            "town": clean_text(firm["town"], 80) or "your area",
            "sender_name": s.get("SENDER_NAME", "North Star AI"),
            "business_name": s.get("BUSINESS_NAME", "North Star AI"),
            "business_address": s.get("BUSINESS_ADDRESS", ""),
            "booking_link": s.get("BOOKING_LINK", "[booking link not set]"),
        }
        subject = clean_text(Template(row["subject"]).safe_substitute(values), 200)
        body = Template(row["body"]).safe_substitute(values)
        body = re.sub(r"\n{3,}", "\n\n", body)
        return subject, body, row["body_hash"]
