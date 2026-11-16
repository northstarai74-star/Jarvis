"""Time helpers. All stored timestamps are UTC ISO strings ending in Z.

UK local time is worked out by hand (GMT/BST) because the standard library
has no time zone database on Windows.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

UTC = timezone.utc
WINDOW_START = time(9, 30)
WINDOW_END = time(16, 30)


def _last_sunday(year: int, month: int) -> date:
    d = date(year, month, 31)  # March and October both have 31 days
    return d - timedelta(days=(d.weekday() + 1) % 7)


def uk_offset(dt_utc: datetime) -> timedelta:
    """BST runs from 01:00 UTC on the last Sunday of March to the same in October."""
    start = datetime.combine(_last_sunday(dt_utc.year, 3), time(1), UTC)
    end = datetime.combine(_last_sunday(dt_utc.year, 10), time(1), UTC)
    return timedelta(hours=1) if start <= dt_utc < end else timedelta(0)


def uk_local(dt_utc: datetime) -> datetime:
    """UTC-aware datetime -> naive UK wall-clock datetime."""
    dt_utc = dt_utc.astimezone(UTC)
    return (dt_utc + uk_offset(dt_utc)).replace(tzinfo=None)


def uk_to_utc(local: datetime) -> datetime:
    """Naive UK wall-clock datetime -> UTC-aware datetime."""
    candidate = (local - timedelta(hours=1)).replace(tzinfo=UTC)
    if uk_offset(candidate) == timedelta(hours=1):
        return candidate
    return local.replace(tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def in_send_window(dt_utc: datetime) -> bool:
    """Weekdays, 09:30 up to (not including) 16:30 UK time."""
    local = uk_local(dt_utc)
    return local.weekday() < 5 and WINDOW_START <= local.time() < WINDOW_END


def window_end_utc(dt_utc: datetime) -> datetime:
    local = uk_local(dt_utc)
    return uk_to_utc(datetime.combine(local.date(), WINDOW_END))


def window_start_utc(dt_utc: datetime) -> datetime:
    local = uk_local(dt_utc)
    return uk_to_utc(datetime.combine(local.date(), WINDOW_START))


def day_bounds_utc(dt_utc: datetime) -> tuple:
    """Start and end (UTC) of the UK calendar day that contains dt_utc."""
    local = uk_local(dt_utc)
    start = uk_to_utc(datetime.combine(local.date(), time(0)))
    end = uk_to_utc(datetime.combine(local.date() + timedelta(days=1), time(0)))
    return start, end


class Clock:
    def now(self) -> datetime:
        return datetime.now(UTC).replace(microsecond=0)

    def now_iso(self) -> str:
        return iso(self.now())

    def local_day(self) -> str:
        return uk_local(self.now()).date().isoformat()


class FakeClock(Clock):
    """A clock tests can set and move."""

    def __init__(self, uk_time: str = "2026-11-16 10:00:00"):
        self._now = uk_to_utc(datetime.strptime(uk_time, "%Y-%m-%d %H:%M:%S"))

    def now(self) -> datetime:
        return self._now

    def set_uk(self, uk_time: str) -> None:
        self._now = uk_to_utc(datetime.strptime(uk_time, "%Y-%m-%d %H:%M:%S"))

    def advance(self, **kwargs) -> None:
        self._now = self._now + timedelta(**kwargs)
