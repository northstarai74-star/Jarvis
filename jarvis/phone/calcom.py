"""The Cal.com booking webhook.

Every request must carry an HMAC-SHA256 of the raw body, made with the
shared secret. No secret configured means every request is turned away.
A new booking is recorded and the owner is phoned with the next steps.

The header name and payload fields follow Cal.com's documented webhook, but
have not been tried against the live service (that needs the secret).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime

from ..clock import UTC, iso, uk_local
from ..sales.crm import clean_text, normalise_email

SIGNATURE_HEADER = "X-Cal-Signature-256"
STATUS_FOR_EVENT = {
    "BOOKING_CREATED": "booked",
    "BOOKING_RESCHEDULED": "booked",
    "BOOKING_CANCELLED": "cancelled",
}


def sign(secret: str, raw_body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def _normalise_time(value) -> str:
    """ISO time from the payload -> our UTC format. Unreadable input is kept as text."""
    text = clean_text(value, 40)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return iso(parsed)


def spoken_time(start_time: str) -> str:
    try:
        local = uk_local(datetime.strptime(start_time, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC))
    except ValueError:
        return start_time or "a time to be confirmed"
    return local.strftime("%A %d %B at %H:%M")


class CalcomWebhook:
    def __init__(self, jarvis, crm, phone, secret: str = ""):
        self.jarvis = jarvis
        self.db = jarvis.db
        self.crm = crm
        self.phone = phone
        self.secret = secret
        jarvis.register("booking.record", self._do_record)

    def handle(self, raw_body: bytes, signature: str) -> tuple:
        """Returns (HTTP status, short message)."""
        if not self.secret:
            return 503, "booking webhook is not configured"
        given = str(signature or "").strip()
        if given.startswith("sha256="):
            given = given[len("sha256="):]
        if not given or not hmac.compare_digest(sign(self.secret, raw_body), given.lower()):
            self.jarvis.audit.append("calcom", "webhook.bad_signature", {})
            return 401, "bad signature"
        try:
            data = json.loads(raw_body.decode("utf-8"))
            event = data.get("triggerEvent")
            payload = data.get("payload") or {}
            if not isinstance(event, str) or not isinstance(payload, dict):
                return 400, "unreadable body"
            attendees = payload.get("attendees") or [{}]
            if not isinstance(attendees, list):
                return 400, "unreadable body"
            attendee = attendees[0] if isinstance(attendees[0], dict) else {}
        except (ValueError, AttributeError, IndexError, TypeError):
            return 400, "unreadable body"

        status = STATUS_FOR_EVENT.get(event)
        if status is None:
            return 200, "ignored"
        uid = clean_text(payload.get("uid"), 100)
        if not uid:
            return 400, "booking has no uid"

        result = self.jarvis.execute(
            "booking.record",
            {
                "uid": uid,
                "status": status,
                "attendee_name": clean_text(attendee.get("name"), 120),
                "attendee_email": normalise_email(attendee.get("email")),
                "title": clean_text(payload.get("title"), 200),
                "start_time": _normalise_time(payload.get("startTime")),
            },
            actor="calcom",
        )
        if not result.ok:
            return 500, "could not record the booking"
        if result.value["new"] and status == "booked":
            self._call_owner(result.value)
        return 200, "ok"

    def _do_record(self, params):
        firm = self.crm.firm_by_email(params["attendee_email"]) if params["attendee_email"] else None
        existing = self.db.one("SELECT * FROM bookings WHERE uid = ?", (params["uid"],))
        if existing:
            self.db.execute(
                "UPDATE bookings SET status = ?, start_time = ? WHERE uid = ?",
                (params["status"], params["start_time"] or existing["start_time"], params["uid"]),
            )
        else:
            self.db.execute(
                "INSERT INTO bookings (uid, firm_id, attendee_name, attendee_email, title, "
                "start_time, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (params["uid"], firm["id"] if firm else None, params["attendee_name"],
                 params["attendee_email"], params["title"], params["start_time"],
                 params["status"], self.jarvis.clock.now_iso()),
            )
        if firm and params["status"] == "booked":
            self.crm.set_firm_status(firm["id"], "booked")
            self.crm.cancel_unsent(firm["id"])  # no more automatic emails to them
        return {
            "new": existing is None,
            "uid": params["uid"],
            "firm": firm["name"] if firm else "",
            "attendee_name": params["attendee_name"],
            "start_time": params["start_time"],
        }

    def _call_owner(self, booking) -> None:
        who = booking["attendee_name"] or "Someone"
        when = spoken_time(booking["start_time"])
        if booking["firm"]:
            brief = f"{who} from {booking['firm']} booked a call for {when}."
            steps = [
                f"Look at {booking['firm']} and their reply before the call",
                "The meeting is in your calendar",
                "No more automatic emails will go to this firm",
            ]
        else:
            brief = f"{who} booked a call for {when}. They are not a firm in the CRM."
            steps = ["Check who they are before the call", "The meeting is in your calendar"]
        self.phone.alert_owner("A new call has been booked", brief, steps, actor="calcom")
