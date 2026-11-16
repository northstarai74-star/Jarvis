"""Keeping the system healthy: backup, doctor and the watchdog."""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from .clock import in_send_window, iso, parse_iso

KEEP_BACKUPS = 14
HEARTBEAT_STALE_MINUTES = 10
ERROR_BURST = 5
ERROR_WINDOW_MINUTES = 10


# --- backup ---------------------------------------------------------------

def make_backup(jarvis) -> str:
    """Copy the database to the backup folder, check the copy, prune old ones.

    The .env file is never copied: keys stay out of backups.
    """
    folder = jarvis.settings.backup_dir
    folder.mkdir(parents=True, exist_ok=True)
    stamp = jarvis.clock.now().strftime("%Y%m%d-%H%M%S")
    dest = folder / f"jarvis-{stamp}.db"
    n = 1
    while dest.exists():
        n += 1
        dest = folder / f"jarvis-{stamp}-{n}.db"
    jarvis.db.backup_to(dest)
    check = sqlite3.connect(str(dest))
    try:
        ok = check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()
    if not ok:
        dest.unlink()
        raise RuntimeError("backup failed its integrity check")
    for old in sorted(folder.glob("jarvis-*.db"))[:-KEEP_BACKUPS]:
        old.unlink()
    jarvis.db.kv_set("last_backup", jarvis.clock.now_iso())
    return str(dest)


def backup(jarvis, actor: str = "owner"):
    return jarvis.execute("backup.create", {}, actor=actor)


# --- doctor ---------------------------------------------------------------

@dataclass
class Check:
    name: str
    level: str  # ok | warn | fail | blocked
    detail: str = ""


def doctor(jarvis) -> list:
    """Look the system over. `blocked` means waiting on a key in .env."""
    s = jarvis.settings
    checks = []

    def add(name, level, detail=""):
        checks.append(Check(name, level, detail))

    version = ".".join(str(v) for v in sys.version_info[:3])
    correct_python = sys.version_info[:2] == (3, 12)
    add("python", "ok" if correct_python else "warn",
        version if correct_python else f"{version} (requires Python 3.12)")
    add("service mode", "ok", "offline: file outbox and injected fakes only; keys do not enable live adapters")

    try:
        probe = s.data_dir / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("data folder", "ok", str(s.data_dir))
    except OSError as exc:
        add("data folder", "fail", f"not writable: {exc}")

    if "onedrive" in str(Path(s.root).resolve()).lower():
        add("location", "warn", "project is inside OneDrive; move it out")
    else:
        add("location", "ok", "not in OneDrive")

    add("database", "ok" if jarvis.db.integrity_ok() else "fail")

    result = jarvis.audit.verify()
    add("audit chain", "ok" if result.ok else "fail",
        f"{result.count} entries" if result.ok else f"entry {result.bad_id}: {result.reason}")

    for purpose in ("approval", "voice"):
        if not jarvis.pins.is_set(purpose):
            add(f"{purpose} PIN", "fail", f"not set: run `pin set {purpose}`")
        elif jarvis.pins.is_locked(purpose):
            add(f"{purpose} PIN", "warn", "locked after wrong attempts")
        else:
            add(f"{purpose} PIN", "ok", "set")

    add("hard stop", "warn" if jarvis.is_stopped() else "ok",
        "STOP is on" if jarvis.is_stopped() else "off")

    approved = jarvis.db.scalar("SELECT COUNT(*) FROM templates WHERE approved = 1")
    total = jarvis.db.scalar("SELECT COUNT(*) FROM templates")
    add("templates", "ok" if approved else "warn", f"{approved} of {total} approved")
    add("go-live", "ok" if jarvis.is_live() else "warn",
        "live" if jarvis.is_live() else "not approved yet")
    add("daily cap", "ok" if jarvis.cap() else "warn",
        str(jarvis.cap()) if jarvis.cap() else "not approved yet")

    last = jarvis.db.kv_get("last_backup")
    if not last:
        add("backup", "warn", "no backup yet")
    elif jarvis.clock.now() - parse_iso(last) > timedelta(days=2):
        add("backup", "warn", f"last backup {last}")
    else:
        add("backup", "ok", f"last backup {last}")

    add(".env file", "ok" if (s.root / ".env").is_file() else "warn",
        "present" if (s.root / ".env").is_file() else "missing: copy .env.example to .env")

    missing = s.missing_keys()
    from .settings import KEY_GROUPS
    for group in KEY_GROUPS:
        if group in missing:
            add(group, "blocked", "missing " + ", ".join(missing[group]))
        else:
            add(group, "ok", "keys present (not tested against the live service)")
    return checks


# --- watchdog -------------------------------------------------------------

def heartbeat(jarvis) -> None:
    jarvis.db.kv_set("heartbeat", jarvis.clock.now_iso())


def watchdog(jarvis, alert=None) -> list:
    """Look for trouble. Serious trouble trips the hard stop.

    `alert(reason)` is called once per problem found (for example to phone
    the owner). Returns the list of problems.
    """
    problems = []
    serious = []
    now = jarvis.clock.now()

    if not jarvis.db.integrity_ok():
        serious.append("database failed its integrity check")

    result = jarvis.audit.verify()
    if not result.ok:
        serious.append(f"audit chain broken at entry {result.bad_id}: {result.reason}")

    cap = jarvis.cap()
    if cap and jarvis.sends_today() > cap:
        serious.append("more emails were sent today than the cap allows")

    since = iso(now - timedelta(minutes=ERROR_WINDOW_MINUTES))
    errors = len(jarvis.audit.since(since, "action.error"))
    if errors >= ERROR_BURST:
        serious.append(f"{errors} actions failed in {ERROR_WINDOW_MINUTES} minutes")

    beat = jarvis.db.kv_get("heartbeat")
    if jarvis.is_live() and in_send_window(now) and not jarvis.is_stopped():
        if not beat or now - parse_iso(beat) > timedelta(minutes=HEARTBEAT_STALE_MINUTES):
            problems.append("autopilot has not ticked for over 10 minutes")

    if serious and not jarvis.is_stopped():
        jarvis.stop("watchdog: " + serious[0], actor="watchdog")

    problems = serious + problems
    last_alert = jarvis.db.kv_get("watchdog_last_alert", "")
    signature = jarvis.clock.local_day() + "|" + "|".join(problems)
    if problems and alert is not None and signature != last_alert:
        jarvis.db.kv_set("watchdog_last_alert", signature)
        alert("Watchdog: " + "; ".join(problems))
    if problems:
        jarvis.audit.append("watchdog", "watchdog.problems", {"problems": problems})
    return problems
