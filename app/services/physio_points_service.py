"""Physio Points loyalty programme (Section P2-3).

Rules
-----
* Earning   : floor(cash_paid × 10%) points per payment split whose method is
              NOT POINTS. Applied per split, not per bill total.
              Points earned on amount paid AFTER discounts and points redemptions —
              the per-split approach naturally achieves this (POINTS splits skipped,
              discount already deducted from bill total before payment splits are made).
* Rate      : 10 pts per ₹100 paid (= 10%).
* Redemption: 1 pt = ₹0.50 (PHYSIO_POINTS_REDEEM_VALUE = 0.5).
              A POINTS-method split debits the ledger and earns nothing.
* Refund    : On approval, revoke earned pts for that bill (floor at 0), and
              restore pts that were redeemed on it.
* Expiry    : Points expire 12 months (PHYSIO_POINTS_EXPIRY_DAYS) after earning.
              Lazy expiry — expired debits are created on-demand via
              expire_stale_points() before any balance read or redemption.
* Manual    : Admins can credit/debit with a reason.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.enums import PaymentMethod, PointsTransactionType
from app.models.patient import Patient
from app.models.physio_points import PhysioPointsLedger
from app.utils.exceptions import ValidationError
from app.utils.timezone import local_today

logger = logging.getLogger(__name__)


def _earn_rate() -> Decimal:
    """Earn rate as a multiplier, derived from PHYSIO_POINTS_EARN_PER_100.

    PHYSIO_POINTS_EARN_PER_100 = 10  →  0.10  (10 pts per ₹100)
    PHYSIO_POINTS_EARN_PER_100 = 5   →  0.05  (5 pts per ₹100)
    Set to 0 to disable earning entirely.
    """
    return Decimal(str(settings.PHYSIO_POINTS_EARN_PER_100)) / Decimal("100")


def _pts(amount) -> int:
    """Points earned on a given payment amount, using the configured earn rate."""
    return int((Decimal(str(amount)) * _earn_rate()).to_integral_value(ROUND_DOWN))


# --------------------------------------------------------------------------- #
# Expiry (lazy — runs on demand before balance reads / redemptions)
# --------------------------------------------------------------------------- #

def expire_stale_points(db: Session, patient_id: int) -> int:
    """Create EXPIRED debit entries for any EARNED points past their expiry date.

    Uses lazy evaluation: called before balance reads and redemptions.
    Returns total points expired this call (0 if nothing expired).
    Safe to call multiple times — already-expired entries are tracked via
    source_entry_id so they won't be double-expired.
    """
    today = local_today()

    # IDs of EARNED entries that have already been expired
    already_expired_ids = set(
        db.execute(
            select(PhysioPointsLedger.source_entry_id).where(
                PhysioPointsLedger.patient_id == patient_id,
                PhysioPointsLedger.transaction_type == PointsTransactionType.EXPIRED,
                PhysioPointsLedger.source_entry_id.is_not(None),
            )
        ).scalars().all()
    )

    overdue = db.execute(
        select(PhysioPointsLedger).where(
            PhysioPointsLedger.patient_id == patient_id,
            PhysioPointsLedger.transaction_type == PointsTransactionType.EARNED,
            PhysioPointsLedger.expires_at.is_not(None),
            PhysioPointsLedger.expires_at <= today,
            PhysioPointsLedger.id.not_in(already_expired_ids or {-1}),
        ).order_by(PhysioPointsLedger.created_at)
    ).scalars().all()

    if not overdue:
        return 0

    patient = db.get(Patient, patient_id)
    if patient is None:
        return 0

    total_expired = 0
    remaining_balance = patient.physio_points
    for entry in overdue:
        if remaining_balance <= 0:
            break
        # Cap at current balance so we never go negative
        to_expire = min(entry.points, remaining_balance)
        if to_expire <= 0:
            continue
        db.add(PhysioPointsLedger(
            patient_id=patient_id,
            points=-to_expire,
            transaction_type=PointsTransactionType.EXPIRED,
            source_entry_id=entry.id,
            description=(
                f"Expired {to_expire} pts earned on "
                f"{entry.created_at.strftime('%d %b %Y')} "
                f"(12-month expiry policy)"
            ),
        ))
        patient.physio_points -= to_expire
        remaining_balance -= to_expire
        total_expired += to_expire
        logger.info(
            "Physio Points: expired %d pts for patient %d (source entry %d)",
            to_expire, patient_id, entry.id,
        )

    if total_expired:
        db.flush()
    return total_expired


# --------------------------------------------------------------------------- #
# Earning
# --------------------------------------------------------------------------- #

def earn_from_payment(db: Session, payment, bill, actor) -> int:
    """Credit points for a single non-POINTS payment split.

    Call this once per Payment row immediately after it is flushed.
    Returns number of points credited (0 if nothing was earned).
    """
    if payment.payment_method == PaymentMethod.POINTS:
        return 0  # redemption, not earning

    pts = _pts(payment.amount)
    if pts <= 0:
        return 0

    patient = db.get(Patient, bill.patient_id)
    if patient is None:
        return 0

    bill_ref = getattr(bill, "bill_number", str(bill.id))
    earned_date = getattr(payment, "payment_date", None) or local_today()
    expiry_days = settings.PHYSIO_POINTS_EXPIRY_DAYS
    entry = PhysioPointsLedger(
        patient_id=patient.id,
        points=pts,
        transaction_type=PointsTransactionType.EARNED,
        payment_id=payment.id,
        bill_id=bill.id,
        expires_at=earned_date + timedelta(days=expiry_days),
        description=f"Earned on ₹{payment.amount} payment — bill {bill_ref}",
        created_by_user_id=actor.id if actor else None,
    )
    db.add(entry)
    patient.physio_points += pts
    logger.debug("Physio Points: +%d for patient %d (bill %s)", pts, patient.id, bill_ref)
    return pts


def redeem_from_payment(db: Session, payment, bill, actor) -> None:
    """Debit points for a POINTS-method payment split.

    The payment.amount is the rupee value of the redemption. Points deducted =
    payment.amount / PHYSIO_POINTS_REDEEM_VALUE. At the default of 1.0 this is 1:1.

    Call this once per POINTS Payment row after it is flushed.
    Raises ValidationError if the patient's balance is insufficient.
    """
    if payment.payment_method != PaymentMethod.POINTS:
        return

    redeem_value = Decimal(str(settings.PHYSIO_POINTS_REDEEM_VALUE))
    pts = int((Decimal(str(payment.amount)) / redeem_value).to_integral_value(ROUND_DOWN))
    if pts <= 0:
        return

    # Expire stale points before checking balance so the check is accurate
    expire_stale_points(db, bill.patient_id)

    patient = db.get(Patient, bill.patient_id)
    if patient is None:
        raise ValidationError("Patient not found for points redemption")

    if pts > patient.physio_points:
        raise ValidationError(
            f"Cannot redeem {pts} Physio Points — only {patient.physio_points} available"
        )

    bill_ref = getattr(bill, "bill_number", str(bill.id))
    entry = PhysioPointsLedger(
        patient_id=patient.id,
        points=-pts,
        transaction_type=PointsTransactionType.REDEEMED,
        payment_id=payment.id,
        bill_id=bill.id,
        description=f"Redeemed {pts} pts on bill {bill_ref} (₹{pts} discount)",
        created_by_user_id=actor.id if actor else None,
    )
    db.add(entry)
    patient.physio_points -= pts
    logger.debug("Physio Points: -%d for patient %d (bill %s)", pts, patient.id, bill_ref)


# --------------------------------------------------------------------------- #
# Refund reversal
# --------------------------------------------------------------------------- #

def reverse_on_refund(db: Session, bill_id: int, refund_id: int, actor) -> None:
    """On refund approval, reverse all points transactions for a bill.

    * EARNED entries: revoke up to current balance (no negatives).
    * REDEEMED entries: restore (give the points back).
    """
    ledger_rows = db.execute(
        select(PhysioPointsLedger).where(PhysioPointsLedger.bill_id == bill_id)
    ).scalars().all()

    if not ledger_rows:
        return

    # Group by patient (should always be one patient per bill)
    patient_ids = {r.patient_id for r in ledger_rows}
    for pid in patient_ids:
        patient = db.get(Patient, pid)
        if patient is None:
            continue

        rows_for_patient = [r for r in ledger_rows if r.patient_id == pid]
        earned = sum(r.points for r in rows_for_patient if r.transaction_type == PointsTransactionType.EARNED)
        redeemed = sum(-r.points for r in rows_for_patient if r.transaction_type == PointsTransactionType.REDEEMED)

        # Revoke earned — but don't go below 0
        to_revoke = min(earned, patient.physio_points)
        if to_revoke > 0:
            db.add(PhysioPointsLedger(
                patient_id=pid,
                points=-to_revoke,
                transaction_type=PointsTransactionType.REFUND_REVOKE,
                bill_id=bill_id,
                refund_id=refund_id,
                description=f"Revoked {to_revoke} pts earned on bill (refund #{refund_id})",
                created_by_user_id=actor.id if actor else None,
            ))
            patient.physio_points -= to_revoke

        # Restore redeemed — give back what was spent on this bill
        if redeemed > 0:
            db.add(PhysioPointsLedger(
                patient_id=pid,
                points=redeemed,
                transaction_type=PointsTransactionType.REFUND_RESTORE,
                bill_id=bill_id,
                refund_id=refund_id,
                description=f"Restored {redeemed} pts redeemed on bill (refund #{refund_id})",
                created_by_user_id=actor.id if actor else None,
            ))
            patient.physio_points += redeemed


# --------------------------------------------------------------------------- #
# Manual adjustment (admin)
# --------------------------------------------------------------------------- #

def manual_adjust(db: Session, patient_id: int, points: int, reason: str, actor) -> PhysioPointsLedger:
    """Credit (positive) or debit (negative) points with a reason. Admin only."""
    if points == 0:
        raise ValidationError("Adjustment points cannot be zero")
    if not reason or not reason.strip():
        raise ValidationError("A reason is required for manual adjustments")

    patient = db.get(Patient, patient_id)
    if patient is None:
        raise ValidationError("Patient not found")

    if points < 0 and abs(points) > patient.physio_points:
        raise ValidationError(
            f"Cannot debit {abs(points)} pts — only {patient.physio_points} available"
        )

    txn_type = PointsTransactionType.MANUAL_CREDIT if points > 0 else PointsTransactionType.MANUAL_DEBIT
    entry = PhysioPointsLedger(
        patient_id=patient_id,
        points=points,
        transaction_type=txn_type,
        description=reason.strip(),
        created_by_user_id=actor.id if actor else None,
    )
    db.add(entry)
    patient.physio_points += points
    db.flush()
    return entry


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #

def get_balance(db: Session, patient_id: int) -> int:
    expire_stale_points(db, patient_id)
    patient = db.get(Patient, patient_id)
    return patient.physio_points if patient else 0


def get_ledger(db: Session, patient_id: int, limit: int = 100) -> list[PhysioPointsLedger]:
    return list(
        db.execute(
            select(PhysioPointsLedger)
            .where(PhysioPointsLedger.patient_id == patient_id)
            .order_by(PhysioPointsLedger.created_at.desc())
            .limit(limit)
        ).scalars().all()
    )
