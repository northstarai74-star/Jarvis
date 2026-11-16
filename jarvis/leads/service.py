"""Lead intake and verification.

Statuses:
  needs_email  no email address yet
  found        has an email, not yet checked
  ready        passed every rule; may be promoted to the CRM
  review       a person needs to look (something is missing or unclear)
  rejected     failed a rule (not a limited company, dissolved, suppressed)
  imported     promoted to the CRM as a firm

A lead is ready only if it is an active limited company, has a valid email
with a source for where the email was found, and is not suppressed.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from ..sales.crm import clean_text, normalise_email, valid_email
from .companies_house import LIMITED_TYPES, normalise_name, normalise_number

STATUSES = ("needs_email", "found", "ready", "review", "rejected", "imported")
MAX_ROWS = 5000
MAX_FILE_BYTES = 5 * 1024 * 1024

# Column names seen in exported lead sheets, mapped to our fields.
ALIASES = {
    "name": {"name", "company", "company_name", "firm", "firm_name", "agency",
             "agency_name", "business", "business_name"},
    "company_number": {"company_number", "companies_house_number", "company_no",
                       "crn", "registration_number", "company_reg"},
    "email": {"email", "email_address", "contact_email", "e_mail"},
    "email_source": {"email_source", "source_url", "email_source_url", "source",
                     "found_at", "email_found_at"},
    "website": {"website", "url", "site", "web"},
    "town": {"town", "city", "location", "area"},
    "phone": {"phone", "telephone", "phone_number", "tel"},
}

PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.co.uk", "outlook.com",
    "live.com", "live.co.uk", "yahoo.com", "yahoo.co.uk", "icloud.com", "me.com",
    "aol.com", "btinternet.com", "sky.com", "msn.com", "proton.me", "protonmail.com",
}


def _field_for(header: str):
    key = "".join(ch if ch.isalnum() else "_" for ch in str(header).strip().lower()).strip("_")
    while "__" in key:
        key = key.replace("__", "_")
    for field, names in ALIASES.items():
        if key in names:
            return field
    return None


def parse_rows(text: str, kind: str) -> list:
    """Turn CSV or JSON text into a list of dicts using our field names."""
    if kind == "json":
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("leads", [])
        if not isinstance(data, list):
            raise ValueError("JSON must be a list of leads")
        raw_rows = [r for r in data if isinstance(r, dict)]
    else:
        raw_rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    rows = []
    for raw in raw_rows[:MAX_ROWS]:
        row = {}
        for header, value in raw.items():
            field = _field_for(header) if header is not None else None
            if field and field not in row and value is not None:
                row[field] = value if isinstance(value, str) else str(value)
        rows.append(row)
    return rows


class Leads:
    def __init__(self, jarvis, crm, companies_house=None):
        self.jarvis = jarvis
        self.db = jarvis.db
        self.crm = crm
        self.companies_house = companies_house
        jarvis.register("lead.verify", self._do_verify)
        jarvis.register("lead.promote", self._do_promote)

    # --- reading -----------------------------------------------------------

    def get(self, lead_id):
        return self.db.one("SELECT * FROM leads WHERE id = ?", (lead_id,))

    def list(self, status=None) -> list:
        if status:
            return self.db.query("SELECT * FROM leads WHERE status = ? ORDER BY id", (status,))
        return self.db.query("SELECT * FROM leads ORDER BY id")

    def counts(self) -> dict:
        counts = {status: 0 for status in STATUSES}
        for row in self.db.query("SELECT status, COUNT(*) AS n FROM leads GROUP BY status"):
            counts[row["status"]] = row["n"]
        return counts

    # --- import ------------------------------------------------------------

    def import_file(self, path, actor: str = "owner"):
        path = Path(path)
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("file is too large")
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        kind = "json" if path.suffix.lower() == ".json" else "csv"
        return self.import_rows(parse_rows(text, kind), source=path.name, actor=actor)

    def import_rows(self, rows: list, source: str = "manual", actor: str = "owner"):
        """Store leads. Imported text is data only; nothing in it is acted on."""
        digest = hashlib.sha256(
            json.dumps(rows, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return self.jarvis.execute(
            "lead.import",
            {"source": clean_text(source, 100), "rows": len(rows), "sha256": digest},
            handler=lambda params: self._import(rows, params["source"]),
            actor=actor,
        )

    def _import(self, rows, source):
        result = {"imported": 0, "duplicates": 0, "skipped": 0}
        now = self.jarvis.clock.now_iso()
        with self.db.transaction():
            for row in rows[:MAX_ROWS]:
                name = clean_text(row.get("name"), 160)
                if not name:
                    result["skipped"] += 1
                    continue
                email = normalise_email(row.get("email"))
                number = normalise_number(clean_text(row.get("company_number"), 20))
                if self._is_duplicate(name, email, number):
                    result["duplicates"] += 1
                    continue
                self.db.execute(
                    "INSERT INTO leads (name, company_number, email, email_source, website, "
                    "town, phone, status, source, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (name, number, email, clean_text(row.get("email_source"), 500),
                     clean_text(row.get("website"), 300), clean_text(row.get("town"), 80),
                     clean_text(row.get("phone"), 30),
                     "found" if email else "needs_email", source, now, now),
                )
                result["imported"] += 1
        return result

    def _is_duplicate(self, name, email, number) -> bool:
        if email and self.db.one("SELECT 1 FROM leads WHERE email = ?", (email,)):
            return True
        if number and self.db.one("SELECT 1 FROM leads WHERE company_number = ?", (number,)):
            return True
        wanted = normalise_name(name)
        return any(
            normalise_name(r["name"]) == wanted for r in self.db.query("SELECT name FROM leads")
        )

    def set_email(self, lead_id, email, email_source, actor: str = "owner"):
        """Add a found email (with where it was found) to a needs_email lead."""
        def handler(params):
            self.db.execute(
                "UPDATE leads SET email = ?, email_source = ?, status = 'found', reason = '', "
                "updated_at = ? WHERE id = ? AND status IN ('needs_email', 'review', 'found')",
                (normalise_email(email), clean_text(email_source, 500),
                 self.jarvis.clock.now_iso(), lead_id),
            )
            return "found"
        return self.jarvis.execute(
            "lead.import", {"source": "email added", "rows": 1, "lead_id": lead_id},
            handler=handler, actor=actor,
        )

    # --- verification ------------------------------------------------------

    def verify(self, lead_id, actor: str = "jarvis"):
        return self.jarvis.execute("lead.verify", {"lead_id": lead_id}, actor=actor)

    def verify_all(self, actor: str = "jarvis") -> dict:
        """Check every lead that is waiting. Returns how many landed in each status."""
        tally = {}
        for lead in self.db.query(
            "SELECT id FROM leads WHERE status IN ('found', 'needs_email', 'review') ORDER BY id"
        ):
            result = self.verify(lead["id"], actor=actor)
            status = result.value["status"] if result.ok else "error"
            tally[status] = tally.get(status, 0) + 1
        return tally

    def _judge(self, lead) -> tuple:
        """Apply the rules. Returns (status, reason, company or None)."""
        email = lead["email"] or ""
        if not email:
            return "needs_email", "no email address yet", None
        if not valid_email(email):
            return "review", "the email address looks wrong", None
        if self.crm.is_suppressed(email):
            return "rejected", "on the suppression list", None
        if self.companies_house is None:
            return "review", "Companies House check is not connected", None

        if lead["company_number"]:
            company = self.companies_house.lookup(lead["company_number"])
            if company is None:
                return "review", "company number not found at Companies House", None
            if normalise_name(company["name"]) != normalise_name(lead["name"]):
                return "review", "company number belongs to a different name", None
        else:
            matches = self.companies_house.search(lead["name"])
            if not matches:
                return "review", "no company of this name at Companies House", None
            if len(matches) > 1:
                return "review", "more than one company has this name", None
            company = matches[0]

        if company["status"] != "active":
            return "rejected", f"company is {company['status']}", company
        if company["type"] not in LIMITED_TYPES:
            return "rejected", "not a limited company", company
        if not (lead["email_source"] or "").strip():
            return "review", "no source for the email address", company
        if email.rsplit("@", 1)[-1] in PERSONAL_DOMAINS:
            return "review", "personal email address, not a company one", company
        return "ready", "", company

    def _do_verify(self, params):
        lead = self.get(params["lead_id"])
        if lead is None:
            raise ValueError("no such lead")
        if lead["status"] == "imported":
            raise ValueError("this lead is already in the CRM")
        status, reason, company = self._judge(lead)
        self.db.execute(
            "UPDATE leads SET status = ?, reason = ?, company_number = ?, company_type = ?, "
            "updated_at = ? WHERE id = ?",
            (status, reason,
             company["number"] if company else lead["company_number"],
             company["type"] if company else lead["company_type"],
             self.jarvis.clock.now_iso(), lead["id"]),
        )
        return {"lead_id": lead["id"], "status": status, "reason": reason}

    # --- promotion ---------------------------------------------------------

    def promote(self, lead_id, actor: str = "jarvis"):
        return self.jarvis.execute("lead.promote", {"lead_id": lead_id}, actor=actor)

    def promote_ready(self, actor: str = "jarvis") -> int:
        return sum(1 for lead in self.list("ready") if self.promote(lead["id"], actor).ok)

    def _do_promote(self, params):
        lead = self.get(params["lead_id"])
        if lead is None:
            raise ValueError("no such lead")
        if lead["status"] != "ready":
            raise ValueError(f"only a ready lead can be promoted (this one is {lead['status']})")
        if self.crm.is_suppressed(lead["email"]):
            raise ValueError("this lead is on the suppression list")
        status, reason, _ = self._judge(lead)
        if status != "ready":
            self._do_verify(params)
            raise ValueError(f"lead no longer passes verification: {reason}")
        if self.crm.firm_by_email(lead["email"]):
            raise ValueError("a firm with this email is already in the CRM")
        with self.db.transaction():
            firm_id = self.crm.add_firm(
                lead["name"], lead["email"], lead["company_number"], lead["email_source"],
                lead["town"], lead_id=lead["id"],
            )
            self.db.execute(
                "UPDATE leads SET status = 'imported', updated_at = ? WHERE id = ?",
                (self.jarvis.clock.now_iso(), lead["id"]),
            )
        return {"lead_id": lead["id"], "firm_id": firm_id, "name": lead["name"]}
