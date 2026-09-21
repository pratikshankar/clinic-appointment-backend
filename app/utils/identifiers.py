"""Human-readable identifier generation (Patient ID, bill number, ...).

The sequences are derived from the current maximum in the table *inside the
caller's transaction*, and every generated value is also protected by a UNIQUE
constraint. On SQLite that combination is sufficient. On PostgreSQL, high
concurrency would be better served by a dedicated sequence -- noted in the
README's production section rather than pretended away here.
"""

import re
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

PATIENT_CODE_PREFIX = "PT"
PATIENT_CODE_WIDTH = 6
BILL_NUMBER_PREFIX = "INV"
APPOINTMENT_CODE_PREFIX = "APT"


def _next_sequence(db: Session, column, pattern: re.Pattern[str]) -> int:
    """Highest numeric suffix currently stored in `column`, plus one."""
    highest = 0
    for (value,) in db.execute(select(column)).all():
        if not value:
            continue
        match = pattern.match(value)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def generate_patient_code(db: Session) -> str:
    """Next Patient ID, e.g. ``PT-000001`` (Section 7)."""
    from app.models import Patient

    pattern = re.compile(rf"^{PATIENT_CODE_PREFIX}-(\d+)$")
    sequence = _next_sequence(db, Patient.patient_code, pattern)
    return f"{PATIENT_CODE_PREFIX}-{sequence:0{PATIENT_CODE_WIDTH}d}"


def generate_bill_number(
    db: Session, on_date: date | None = None, series: str | None = None
) -> str:
    """Next bill number, e.g. ``INV-2026-000001``, restarting each year.

    `series` gives a clinic its own sequence (``PC-2026-000001``). A separately
    registered entity needs an unbroken run of its own: sharing one series across
    two entities leaves gaps in both sets of books, and a missing invoice number
    is the first thing an auditor asks about. Because the sequence is derived
    from numbers already carrying the same prefix, each series counts
    independently and neither can affect the other.
    """
    from app.models import Bill

    year = (on_date or date.today()).year
    prefix = f"{(series or BILL_NUMBER_PREFIX).strip().upper()}-{year}"
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    sequence = _next_sequence(db, Bill.bill_number, pattern)
    return f"{prefix}-{sequence:06d}"


def generate_appointment_code(db: Session, on_date: date, clinic_code: str) -> str:
    """Appointment reference, e.g. ``APT-HSR-20260820-0007``.

    The clinic code is part of the sequence key on purpose. A date-only sequence
    is shared by every clinic, so two receptionists at *different* branches
    booking at the same instant compute the same number and one hits the UNIQUE
    constraint -- surfacing as a confusing conflict rather than a clean result.

    Scoping the sequence per clinic makes the booking lock sufficient: same-clinic
    bookings are already serialised by `appointment_service._lock_clinic`, and
    different clinics can no longer collide because their prefixes differ.
    """
    from app.models import Appointment

    prefix = f"{APPOINTMENT_CODE_PREFIX}-{clinic_code.upper()}-{on_date.strftime('%Y%m%d')}"
    count = db.execute(
        select(func.count())
        .select_from(Appointment)
        .where(Appointment.appointment_code.like(f"{prefix}-%"))
    ).scalar_one()
    return f"{prefix}-{count + 1:04d}"


def slugify_clinic_code(name: str, existing: set[str] | None = None) -> str:
    """Derive a short clinic code from its name, e.g. "HSR Layout" -> "HSRL"."""
    words = re.findall(r"[A-Za-z0-9]+", name.upper())
    if not words:
        base = "CLINIC"
    elif len(words) == 1:
        base = words[0][:4]
    else:
        base = "".join(word[0] for word in words)[:4]
        if len(base) < 3:
            base = words[0][:4]

    existing = existing or set()
    if base not in existing:
        return base
    suffix = 2
    while f"{base}{suffix}" in existing:
        suffix += 1
    return f"{base}{suffix}"
