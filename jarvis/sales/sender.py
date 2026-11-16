"""Offline email senders. File outbox plus an injectable SMTP test interface.

A sender only delivers. Whether an email may be sent at all is decided
before this, in Jarvis.execute.
"""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import formataddr, format_datetime, make_msgid

from .crm import valid_email


def build_message(from_name, from_email, to, subject, body, when) -> EmailMessage:
    if not valid_email(to):
        raise ValueError("not a valid recipient address")
    if any(ch in subject for ch in "\r\n"):
        raise ValueError("the subject must be a single line")
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_email))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = format_datetime(when)
    msg["Message-ID"] = make_msgid(domain=from_email.rsplit("@", 1)[-1])
    msg.set_content(body)
    return msg


class FileOutboxSender:
    """Writes each email to a .eml file instead of sending it. Safe for dry runs."""

    name = "file outbox"

    def __init__(self, folder, from_name, from_email, clock):
        self.folder = folder
        self.from_name = from_name
        self.from_email = from_email or "outbox@jarvis.invalid"
        self.clock = clock

    def send(self, to: str, subject: str, body: str) -> str:
        msg = build_message(self.from_name, self.from_email, to, subject, body,
                            self.clock.now())
        self.folder.mkdir(parents=True, exist_ok=True)
        stamp = self.clock.now().strftime("%Y%m%d-%H%M%S")
        n = len(list(self.folder.glob("*.eml"))) + 1
        path = self.folder / f"{stamp}-{n:04d}.eml"
        path.write_bytes(bytes(msg))
        return msg["Message-ID"]


class SmtpSender:
    """Exercises the future SMTP protocol using an explicitly injected fake."""

    name = "SMTP"

    def __init__(self, host, port, user, password, from_name, from_email, clock,
                 smtp_factory=None):
        if smtp_factory is None:
            raise RuntimeError("Live SMTP is disabled; inject a fake SMTP factory for tests")
        self.host, self.port, self.user, self.password = host, int(port), user, password
        self.from_name, self.from_email, self.clock = from_name, from_email, clock
        self.smtp_factory = smtp_factory

    def send(self, to: str, subject: str, body: str) -> str:
        msg = build_message(self.from_name, self.from_email, to, subject, body,
                            self.clock.now())
        with self.smtp_factory(self.host, self.port, timeout=30) as server:
            server.starttls()
            server.login(self.user, self.password)
            server.send_message(msg)
        return msg["Message-ID"]


def make_sender(settings, clock):
    """Always use the file outbox in this offline build, even if keys exist."""
    name = settings.get("SENDER_NAME", "North Star AI")
    return FileOutboxSender(settings.outbox_dir, name, settings.get("SENDER_EMAIL"), clock)
