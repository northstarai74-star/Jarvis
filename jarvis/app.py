"""Wires the parts together. Outside services are passed in, so tests use fakes."""

from __future__ import annotations

from . import ops
from .autopilot import Autopilot
from .core import Jarvis
from .leads.companies_house import from_settings as ch_from_settings
from .leads.service import Leads
from .phone.calcom import CalcomWebhook
from .phone.service import PhoneService
from .phone.twilio import from_settings as twilio_from_settings
from .sales.crm import Crm
from .sales.sender import make_sender
from .sales.service import Sales
from .sales.templates import Templates


class App:
    def __init__(self, settings, db=None, clock=None, sender=None, companies_house=None,
                 twilio=None):
        self.settings = settings
        self.jarvis = Jarvis(settings, db=db, clock=clock)
        j = self.jarvis
        j.register(
            "backup.create", lambda params: ops.make_backup(j),
            lambda params: "Back up the database",
        )
        self.crm = Crm(j.db, j.clock)
        self.templates = Templates(j)
        self.sender = sender or make_sender(settings, j.clock)
        self.sales = Sales(j, self.crm, self.templates, self.sender)
        self.companies_house = companies_house or ch_from_settings(settings)
        self.leads = Leads(j, self.crm, self.companies_house)
        self.autopilot = Autopilot(j, self.crm, self.sales, self.leads)
        self.twilio = twilio or twilio_from_settings(settings)
        self.phone = PhoneService(j, self.twilio)
        self.calcom = CalcomWebhook(j, self.crm, self.phone,
                                    settings.get("CALCOM_WEBHOOK_SECRET"))
        self.autopilot.on_summary = self._on_summary

    def watchdog(self) -> list:
        """Check for trouble; phone the owner about it if the line is connected."""
        alert = None
        if self.phone.connected:
            alert = lambda reason: self.phone.alert_owner(reason, actor="watchdog")
        return ops.watchdog(self.jarvis, alert=alert)

    def _on_summary(self, kind: str, text: str) -> None:
        """Phone the owner in the morning when something needs their approval."""
        waiting = len(self.jarvis.approvals.pending())
        if kind == "morning" and waiting and self.phone.connected:
            self.phone.alert_owner(
                "Your morning brief",
                f"{waiting} approval{'s are' if waiting != 1 else ' is'} waiting for you.",
                actor="autopilot",
            )

    def close(self) -> None:
        self.jarvis.db.close()
