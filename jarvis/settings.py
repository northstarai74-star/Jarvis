"""Settings and the .env loader. Keys live in .env only."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

SECRET_KEYS = (
    "SMTP_PASSWORD",
    "TWILIO_AUTH_TOKEN",
    "CALCOM_WEBHOOK_SECRET",
    "COMPANIES_HOUSE_API_KEY",
)

# What each outside service needs before it can be switched on.
KEY_GROUPS = {
    "email (SMTP)": ["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SENDER_EMAIL"],
    "phone (Twilio)": [
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_FROM_NUMBER",
        "OWNER_PHONE",
        "PUBLIC_BASE_URL",
    ],
    "booking (Cal.com)": ["CALCOM_WEBHOOK_SECRET", "BOOKING_LINK"],
    "company checks (Companies House)": ["COMPANIES_HOUSE_API_KEY"],
}

DEFAULTS = {
    "BUSINESS_NAME": "North Star AI",
    "SENDER_NAME": "North Star AI",
    "EMAIL_SENDER": "file",
    "SMTP_PORT": "587",
    "MAX_DAILY_CAP": "50",
    "MAX_CALLS_PER_DAY": "6",
    "APPROVAL_TTL_MINUTES": "720",
    "PIN_ITERATIONS": "200000",
    "DASHBOARD_PORT": "8765",
    "WEBHOOK_PORT": "8766",
}

KNOWN_KEYS = set(DEFAULTS) | {k for keys in KEY_GROUPS.values() for k in keys} | {
    "JARVIS_DATA_DIR",
    "BUSINESS_ADDRESS",
}


def parse_env(text: str) -> dict:
    """Parse KEY=VALUE lines. Supports comments, `export`, and quoted values."""
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


@dataclass
class Settings:
    root: Path
    values: dict = field(default_factory=dict)

    @classmethod
    def load(cls, root=None, environ=None) -> "Settings":
        """Read <root>/.env; process environment may override non-secret options.

        An explicit ``environ`` mapping is dependency injection for fake test
        settings. Real process secrets are ignored: keys belong in .env only.
        """
        import os

        root = Path(root) if root else Path.cwd()
        values = dict(DEFAULTS)
        env_file = root / ".env"
        if env_file.is_file():
            values.update(parse_env(env_file.read_text(encoding="utf-8")))
        source = os.environ if environ is None else environ
        for key in KNOWN_KEYS:
            if environ is None and key in SECRET_KEYS:
                continue
            if source.get(key):
                values[key] = source[key]
        return cls(root=root, values=values)

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key) or default

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, str(default)))
        except ValueError:
            return default

    @property
    def data_dir(self) -> Path:
        custom = self.get("JARVIS_DATA_DIR")
        return Path(custom) if custom else self.root / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jarvis.db"

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def stop_file(self) -> Path:
        return self.data_dir / "STOP"

    def secrets(self) -> list:
        """Secret values, so the audit log can scrub them wherever they appear."""
        return [self.values[k] for k in SECRET_KEYS if self.values.get(k)]

    def missing_keys(self) -> dict:
        """Service name -> the keys it still needs. Empty dict means all set."""
        missing = {}
        for group, keys in KEY_GROUPS.items():
            absent = [k for k in keys if not self.values.get(k)]
            if absent:
                missing[group] = absent
        return missing
