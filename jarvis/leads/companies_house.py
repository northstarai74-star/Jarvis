"""Companies House lookups.

Only the fake exists so far. A real client needs COMPANIES_HOUSE_API_KEY and
network access, so it has not been written or tested; `from_settings` returns
None and leads then wait in `review` instead of being passed unchecked.
"""

from __future__ import annotations

import re

# Company types that count as a limited company (a corporate subscriber).
LIMITED_TYPES = {
    "ltd",
    "plc",
    "llp",
    "private-limited-guarant-nsc",
    "private-limited-guarant-nsc-limited-exemption",
    "private-limited-shares-section-30-exemption",
}


def normalise_number(number) -> str:
    text = re.sub(r"\s+", "", str(number or "")).upper()
    return text.zfill(8) if text.isdigit() else text


def normalise_name(name) -> str:
    text = str(name or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\blimited\b", "ltd", text)
    return " ".join(text.split())


class FakeCompaniesHouse:
    """An in-memory register for tests and dry runs."""

    def __init__(self, companies=()):
        self.companies = {}
        self.lookups = 0
        for company in companies:
            self.add(**company)

    def add(self, number, name, type="ltd", status="active") -> None:
        number = normalise_number(number)
        self.companies[number] = {
            "number": number, "name": name, "type": type, "status": status,
        }

    def lookup(self, number):
        self.lookups += 1
        return self.companies.get(normalise_number(number))

    def search(self, name) -> list:
        self.lookups += 1
        wanted = normalise_name(name)
        return [c for c in self.companies.values() if normalise_name(c["name"]) == wanted]


def from_settings(settings):
    """The real register client, once it exists. Today: always None."""
    return None
