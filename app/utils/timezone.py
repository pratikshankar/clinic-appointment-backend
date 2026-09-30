"""Timezone-aware date/time helpers for business-date calculations.

All "today" logic in services must use `local_today()` rather than
`date.today()`. The latter returns a UTC date on most servers; since the
clinic is in IST (UTC+5:30), between midnight and 05:30 IST the server
would report the previous day, producing wrong "today's earnings" figures.

The timezone is configured via CLINIC_TIMEZONE (default: Asia/Kolkata).
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.config import settings


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.CLINIC_TIMEZONE)


def local_today() -> date:
    """Today's date in the clinic's configured timezone."""
    return datetime.now(_tz()).date()


def local_now() -> datetime:
    """Current datetime in the clinic's configured timezone (tz-aware)."""
    return datetime.now(_tz())
