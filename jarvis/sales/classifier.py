"""Sorts a reply into a category using fixed rules.

A reply is data. Nothing written in it is ever followed as an instruction;
it can only land in one of the categories below.
"""

from __future__ import annotations

import re

CATEGORIES = (
    "stop", "bounce", "auto_reply", "not_interested", "interested", "question", "other",
)

_QUOTE_START = re.compile(
    r"^(on .{5,200} wrote:|-{2,}\s*original message\s*-{2,}|from: .+|sent from my .+)$",
    re.IGNORECASE,
)
_BOUNCE_FROM = re.compile(r"^(mailer-daemon|postmaster)@", re.IGNORECASE)
_BOUNCE = re.compile(
    r"undeliverable|delivery status notification|mail delivery (failed|subsystem)|"
    r"returned mail|delivery has failed|address not found|could not be delivered",
    re.IGNORECASE,
)
_AUTO = re.compile(
    r"out of (the )?office|automatic reply|auto[- ]?reply|autoreply|annual leave|"
    r"away from (the|my) (office|desk)|on holiday until|currently away",
    re.IGNORECASE,
)
_STOP = re.compile(
    r"\b(stop|unsubscribe|opt[ -]?out|remove (me|us)|take (me|us) off|"
    r"do not (contact|email)|don'?t (contact|email)|no more emails?)\b",
    re.IGNORECASE,
)
_NO = re.compile(
    r"\b(not interested|no thanks|no thank you|not for us|not at this time|"
    r"not looking|we are fine|we're fine|already have)\b",
    re.IGNORECASE,
)
_YES = re.compile(
    r"\b(interested|sounds good|tell me more|happy to (chat|talk|speak)|"
    r"let'?s (chat|talk|speak)|call me|give me a call|yes please|keen|"
    r"send (me |us )?(more|details|info)|book(ed)? (a|in)|love to)\b",
    re.IGNORECASE,
)


def fresh_text(body: str) -> str:
    """The part the sender wrote, without the quoted copy of our own email.

    This matters: our footer says "reply STOP", and a quoted copy of it must
    not be read as the sender asking to stop.
    """
    kept = []
    for line in str(body or "").splitlines():
        stripped = line.strip()
        if _QUOTE_START.match(stripped):
            break
        if stripped.startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def classify(from_email: str, subject: str, body: str) -> str:
    subject = str(subject or "")
    text = fresh_text(body)
    if _BOUNCE_FROM.match(str(from_email or "")) or _BOUNCE.search(subject):
        return "bounce"
    if _AUTO.search(subject) or _AUTO.search(text[:400]):
        return "auto_reply"
    if _STOP.search(text) or _STOP.search(subject):
        return "stop"
    if _NO.search(text):
        return "not_interested"
    if _YES.search(text):
        return "interested"
    if "?" in text:
        return "question"
    return "other"
