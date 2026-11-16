"""Calls to the owner, and approvals on the phone keypad.

The call script: Jarvis says why it is calling and reads the brief. If
approvals are waiting, the owner presses 1, types the phone PIN (separate
from the keyboard PIN), then for each approval presses 1 to approve, 2 to
reject or 3 to skip. An approved action runs itself through the gate.

Only calls Jarvis itself placed are recognised, and the only number it will
ever dial is OWNER_PHONE from .env.
"""

from __future__ import annotations

import json
from datetime import timedelta

from ..approvals import ApprovalError
from ..clock import parse_iso
from ..pins import PinError, PinLocked
from ..sales.crm import clean_text
from .twilio import gather, hangup, response, say

PIN_SESSION_MINUTES = 10
ENDED = {"completed", "busy", "failed", "no-answer", "canceled"}


class PhoneService:
    def __init__(self, jarvis, twilio=None):
        self.jarvis = jarvis
        self.db = jarvis.db
        self.twilio = twilio
        jarvis.register("call.owner_alert", self._do_call)

    @property
    def connected(self) -> bool:
        s = self.jarvis.settings
        return self.twilio is not None and bool(s.get("OWNER_PHONE"))

    # --- placing the call --------------------------------------------------

    def alert_owner(self, reason: str, brief: str = "", next_steps=(), actor: str = "jarvis"):
        return self.jarvis.execute(
            "call.owner_alert",
            {
                "reason": clean_text(reason, 200),
                "brief": clean_text(brief, 600),
                "next_steps": [clean_text(step, 200) for step in list(next_steps)[:5]],
            },
            actor=actor,
        )

    def _do_call(self, params):
        s = self.jarvis.settings
        if self.twilio is None:
            raise RuntimeError("the phone line is not connected (Twilio keys missing)")
        to = s.get("OWNER_PHONE")
        if not to:
            raise RuntimeError("OWNER_PHONE is not set")
        base = s.get("PUBLIC_BASE_URL").rstrip("/")
        script = params.get("brief", "")
        steps = params.get("next_steps") or []
        if steps:
            script += " Next steps. " + " ".join(
                f"{n}: {step}." for n, step in enumerate(steps, 1))
        sid = self.twilio.create_call(
            to=to, from_=s.get("TWILIO_FROM_NUMBER"),
            url=base + "/twilio/voice", status_callback=base + "/twilio/status",
        )
        now = self.jarvis.clock.now_iso()
        self.db.execute(
            "INSERT INTO calls (sid, kind, to_number, status, reason, brief, day, created_at, "
            "updated_at) VALUES (?, 'owner_alert', ?, 'initiated', ?, ?, ?, ?, ?)",
            (sid, to, params.get("reason", ""), script.strip(), self.jarvis.clock.local_day(),
             now, now),
        )
        return {"sid": sid}

    # --- call state --------------------------------------------------------

    def _call(self, form):
        return self.db.one("SELECT * FROM calls WHERE sid = ?", (str(form.get("CallSid", "")),))

    def _save(self, call, state=None, status=None) -> None:
        self.db.execute(
            "UPDATE calls SET state = ?, status = ?, updated_at = ? WHERE id = ?",
            (json.dumps(state) if state is not None else call["state"],
             status or call["status"], self.jarvis.clock.now_iso(), call["id"]),
        )

    def _authed(self, call, state) -> bool:
        stamp = state.get("authed_at")
        if not stamp or call["status"] in ENDED:
            return False
        age = self.jarvis.clock.now() - parse_iso(stamp)
        return age <= timedelta(minutes=PIN_SESSION_MINUTES)

    @staticmethod
    def _unknown() -> str:
        return response(say("Sorry, this call is not recognised. Goodbye."), hangup())

    # --- webhooks: each returns TwiML --------------------------------------

    def voice(self, form) -> str:
        """The owner picked up."""
        call = self._call(form)
        if call is None:
            return self._unknown()
        self._save(call, status="in-progress")
        business = self.jarvis.settings.get("BUSINESS_NAME", "your business")
        parts = [say(f"Hello. This is Jarvis, calling for {business}."),
                 say(call["reason"] + ".")]
        if call["brief"]:
            parts.append(say(call["brief"]))
        return response(*parts, *self._menu())

    def _menu(self) -> list:
        count = len(self.jarvis.approvals.pending())
        if count == 0:
            return [say("Nothing is waiting for your approval. Goodbye."), hangup()]
        things = "1 thing needs" if count == 1 else f"{count} things need"
        return [
            gather("/twilio/menu",
                   f"{things} your approval. Press 1 to go through them. "
                   "Press 2 to end the call.", num_digits=1),
            say("I did not hear a key. Goodbye."), hangup(),
        ]

    def _pin_prompt(self) -> list:
        return [
            gather("/twilio/pin", "Enter your phone PIN, then press the hash key.",
                   finish_on_key="#"),
            say("I did not hear a PIN. Goodbye."), hangup(),
        ]

    def menu(self, form) -> str:
        call = self._call(form)
        if call is None:
            return self._unknown()
        digit = str(form.get("Digits", ""))
        if digit == "1":
            return response(*self._pin_prompt())
        if digit == "2":
            return response(say("Goodbye."), hangup())
        return response(*self._menu())

    def pin(self, form) -> str:
        call = self._call(form)
        if call is None:
            return self._unknown()
        digits = str(form.get("Digits", ""))
        locked = response(
            say("The phone PIN is locked. Please approve at the keyboard instead. Goodbye."),
            hangup())
        try:
            ok = self.jarvis.pins.verify("voice", digits)
        except PinLocked:
            return locked
        except PinError:
            return response(say("No phone PIN has been set. Goodbye."), hangup())
        if not ok:
            if self.jarvis.pins.is_locked("voice"):
                return locked
            return response(say("That PIN is not right."), *self._pin_prompt())
        state = {"authed_at": self.jarvis.clock.now_iso(), "skipped": [], "current": None}
        self.jarvis.audit.append("owner", "call.pin_ok", {"sid": call["sid"]})
        return response(say("Thank you."), *self._next(call, state))

    def _next(self, call, state) -> list:
        """Read out the next approval, or finish."""
        waiting = [a for a in self.jarvis.approvals.pending() if a["id"] not in state["skipped"]]
        if not waiting:
            state["current"] = None
            self._save(call, state)
            return [say("That is everything. Goodbye."), hangup()]
        approval = waiting[0]
        state["current"] = approval["id"]
        self._save(call, state)
        return [
            gather("/twilio/decision",
                   f"{approval['summary']}. Press 1 to approve, 2 to reject, 3 to skip.",
                   num_digits=1),
            say("I did not hear a key. Goodbye."), hangup(),
        ]

    def decision(self, form) -> str:
        call = self._call(form)
        if call is None:
            return self._unknown()
        state = json.loads(call["state"] or "{}")
        if not self._authed(call, state):
            return response(say("You need to enter your phone PIN first. Goodbye."), hangup())
        current = state.get("current")
        digit = str(form.get("Digits", ""))
        spoken = ""
        if current and digit == "1":
            try:
                self.jarvis.approvals.mark_approved(current, via="phone")
                result = self.jarvis.resume(current)
                spoken = ("Approved and done." if result.ok
                          else "Approved, but it did not run. Check the dashboard.")
            except ApprovalError:
                spoken = "That one could not be approved. It may have expired."
        elif current and digit == "2":
            try:
                self.jarvis.approvals.reject(current, via="phone")
                spoken = "Rejected."
            except ApprovalError:
                spoken = "That one is no longer waiting."
        elif current and digit == "3":
            state["skipped"].append(current)
            spoken = "Skipped."
        parts = [say(spoken)] if spoken else []
        return response(*parts, *self._next(call, state))

    def status(self, form) -> str:
        """Twilio reports the call's progress. Ending a call ends the PIN session."""
        call = self._call(form)
        if call is None:
            return ""
        status = clean_text(form.get("CallStatus", ""), 20)
        if status:
            state = json.loads(call["state"] or "{}")
            if status in ENDED:
                state.pop("authed_at", None)
            self._save(call, state, status=status)
        return ""
