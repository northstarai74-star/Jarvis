"""PIN storage: pbkdf2, constant-time compare, three strikes then a lockout.

Two separate PINs exist: "approval" (typed at the keyboard) and "voice"
(typed on the phone keypad). They must be different.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from .clock import iso, parse_iso

PURPOSES = ("approval", "voice")
MAX_FAILS = 3
LOCKOUT_MINUTES = 15


class PinError(Exception):
    pass


class PinLocked(PinError):
    pass


def _derive(pin: str, salt_hex: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", pin.encode("utf-8"), bytes.fromhex(salt_hex), iterations
    ).hex()


class PinStore:
    def __init__(self, db, clock, audit, iterations: int = 200_000):
        self.db = db
        self.clock = clock
        self.audit = audit
        self.iterations = iterations

    def _row(self, purpose: str):
        if purpose not in PURPOSES:
            raise PinError(f"unknown PIN purpose: {purpose}")
        return self.db.one("SELECT * FROM pins WHERE purpose = ?", (purpose,))

    def is_set(self, purpose: str) -> bool:
        return self._row(purpose) is not None

    def is_locked(self, purpose: str) -> bool:
        row = self._row(purpose)
        if not row or not row["locked_until"]:
            return False
        return parse_iso(row["locked_until"]) > self.clock.now()

    def _matches(self, row, pin: str) -> bool:
        return hmac.compare_digest(_derive(pin, row["salt"], row["iterations"]), row["hash"])

    def set_pin(self, purpose: str, pin: str) -> None:
        if purpose not in PURPOSES:
            raise PinError(f"unknown PIN purpose: {purpose}")
        if not isinstance(pin, str) or not pin.isdigit() or not 4 <= len(pin) <= 8:
            raise PinError("a PIN is 4 to 8 digits")
        other = self._row("voice" if purpose == "approval" else "approval")
        if other and self._matches(other, pin):
            raise PinError("the approval PIN and the voice PIN must be different")
        salt = secrets.token_hex(16)
        digest = _derive(pin, salt, self.iterations)
        self.db.execute(
            "INSERT INTO pins (purpose, salt, hash, iterations, fails, locked_until) "
            "VALUES (?, ?, ?, ?, 0, NULL) ON CONFLICT(purpose) DO UPDATE SET "
            "salt = excluded.salt, hash = excluded.hash, iterations = excluded.iterations, "
            "fails = 0, locked_until = NULL",
            (purpose, salt, digest, self.iterations),
        )
        self.audit.append("owner", "pin.set", {"purpose": purpose})

    def verify(self, purpose: str, pin: str) -> bool:
        """True if the PIN is right. Raises PinLocked during a lockout."""
        with self.db.lock:
            row = self._row(purpose)
            if row is None:
                raise PinError(f"the {purpose} PIN has not been set")
            if row["locked_until"] and parse_iso(row["locked_until"]) > self.clock.now():
                self.audit.append("system", "pin.locked_attempt", {"purpose": purpose})
                raise PinLocked(f"the {purpose} PIN is locked; try again later")
            if isinstance(pin, str) and self._matches(row, pin):
                self.db.execute(
                    "UPDATE pins SET fails = 0, locked_until = NULL WHERE purpose = ?",
                    (purpose,),
                )
                return True
            fails = row["fails"] + 1
            if fails >= MAX_FAILS:
                until = iso(self.clock.now() + timedelta(minutes=LOCKOUT_MINUTES))
                self.db.execute(
                    "UPDATE pins SET fails = 0, locked_until = ? WHERE purpose = ?",
                    (until, purpose),
                )
                self.audit.append("system", "pin.lockout", {"purpose": purpose, "until": until})
            else:
                self.db.execute(
                    "UPDATE pins SET fails = ? WHERE purpose = ?", (fails, purpose)
                )
                self.audit.append("system", "pin.wrong", {"purpose": purpose, "fails": fails})
            return False
