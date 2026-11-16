"""Twilio pieces: a fake client, request signatures and TwiML.

Only the fake client exists. A real one needs the Twilio keys and network
access, so it has not been written or tested; `from_settings` returns None.
The signature check follows Twilio's published scheme (HMAC-SHA1 over the
URL plus the sorted form fields) but has not been tried against live calls.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from xml.sax.saxutils import escape, quoteattr


def twilio_signature(auth_token: str, url: str, params: dict) -> str:
    data = url + "".join(key + str(params[key]) for key in sorted(params))
    digest = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def valid_signature(auth_token: str, url: str, params: dict, signature: str) -> bool:
    """False when there is no token: an unconfigured line trusts nobody."""
    if not auth_token or not signature:
        return False
    return hmac.compare_digest(twilio_signature(auth_token, url, params), signature)


class FakeTwilio:
    """Remembers the calls it was asked to place. Never touches the network."""

    def __init__(self):
        self.calls = []
        self.fail = False

    def create_call(self, to: str, from_: str, url: str, status_callback: str = "") -> str:
        if self.fail:
            raise ConnectionError("phone service unreachable")
        sid = "CA" + secrets.token_hex(16)
        self.calls.append({"sid": sid, "to": to, "from": from_, "url": url,
                           "status_callback": status_callback})
        return sid


def from_settings(settings):
    """The real Twilio client, once it exists. Today: always None."""
    return None


# --- TwiML ----------------------------------------------------------------

def say(text: str) -> str:
    return f'<Say voice="alice" language="en-GB">{escape(str(text))}</Say>'


def gather(action: str, prompt: str, num_digits: int | None = None,
           finish_on_key: str | None = None) -> str:
    attrs = f'input="dtmf" action={quoteattr(action)} method="POST" timeout="8"'
    if num_digits:
        attrs += f' numDigits="{int(num_digits)}"'
    if finish_on_key:
        attrs += f" finishOnKey={quoteattr(finish_on_key)}"
    return f"<Gather {attrs}>{say(prompt)}</Gather>"


def hangup() -> str:
    return "<Hangup/>"


def response(*parts: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(parts) + "</Response>"
