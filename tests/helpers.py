"""Shared test setup. Everything runs in a temp folder with an in-memory database."""

import tempfile
import unittest
from pathlib import Path

from jarvis.app import App
from jarvis.clock import FakeClock
from jarvis.db import Database
from jarvis.leads.companies_house import FakeCompaniesHouse
from jarvis.phone.twilio import FakeTwilio
from jarvis.settings import Settings

# Made-up values so the phone and booking code paths can run. Not real keys.
PHONE_ENV = {
    "OWNER_PHONE": "+447700900001",
    "TWILIO_FROM_NUMBER": "+447700900002",
    "TWILIO_ACCOUNT_SID": "ACtest",
    "TWILIO_AUTH_TOKEN": "test-twilio-token",
    "PUBLIC_BASE_URL": "https://jarvis.example.test",
    "CALCOM_WEBHOOK_SECRET": "test-calcom-secret",
}

# Throwaway PINs that exist only inside tests.
APPROVAL_PIN = "4821"
VOICE_PIN = "7350"

TEST_ENV = {"PIN_ITERATIONS": "1000", "BOOKING_LINK": "https://cal.test/northstar/intro",
            "BUSINESS_ADDRESS": "1 Test Street, Leeds"}


class FakeSender:
    """Stands in for an email service. Remembers what it was asked to send."""

    name = "fake"

    def __init__(self):
        self.sent = []
        self.fail = False

    def send(self, to, subject, body):
        if self.fail:
            raise ConnectionError("mail server unreachable")
        self.sent.append({"to": to, "subject": subject, "body": body})
        return f"<fake-{len(self.sent)}@test>"


class JarvisTestCase(unittest.TestCase):
    env = {}
    start = "2026-11-16 10:00:00"  # a Monday, inside the sending window

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = Settings.load(self.root, environ={**TEST_ENV, **self.env})
        self.clock = FakeClock(self.start)
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.app = self.make_app()
        self.jarvis = self.app.jarvis

    def make_app(self):
        self.sender = FakeSender()
        self.register = FakeCompaniesHouse()
        self.twilio = FakeTwilio()
        return App(self.settings, db=self.db, clock=self.clock, sender=self.sender,
                   companies_house=self.register, twilio=self.twilio)

    def go_live(self, cap=10, templates=("direct", "soft", "followup")):
        """Set PINs, approve the wording, go live and set the cap, all by PIN."""
        if not self.jarvis.pins.is_set("approval"):
            self.set_pins()
        for name in templates:
            self.approve(self.app.templates.request_approval(name))
        self.approve(self.app.sales.request_go_live())
        self.approve(self.app.sales.request_cap(cap))

    def add_firm(self, n=1, **over):
        details = {"name": f"Firm {n} Ltd", "email": f"hello@firm{n}.test",
                   "company_number": f"{n:08d}", "email_source": f"https://firm{n}.test/contact",
                   "town": "Leeds"}
        details.update(over)
        return self.app.crm.add_firm(**details)

    def set_pins(self):
        self.jarvis.pins.set_pin("approval", APPROVAL_PIN)
        self.jarvis.pins.set_pin("voice", VOICE_PIN)

    def approve(self, result):
        """Approve a pending result with the approval PIN and return the run result."""
        self.assertEqual(result.status, "pending", result.reason)
        return self.jarvis.approve(result.approval_id, APPROVAL_PIN)

    def events(self, name=None):
        rows = self.db.query("SELECT event, detail FROM audit_log ORDER BY id")
        return [r for r in rows if name is None or r["event"] == name]
