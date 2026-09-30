"""Bills, line items and payments (Sections 17-19).

Money lives here and nowhere else. In particular, payment details are **not**
stored on the treatment package: the dashboard already computes revenue from
`bills.amount_paid`, so a second place to record money would immediately make
that number wrong.

Everything is `Decimal` quantised to two places -- never float, and never a
Python `round()` on a float, which is how currency totals drift by a paisa and
stop reconciling against the day's takings.
"""

import logging
from datetime import date
from decimal import Decimal

from app.utils.timezone import local_today

from fastapi import Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import permissions
from app.models import (
    AuditAction,
    Bill,
    BillItem,
    BillItemType,
    BillStatus,
    Clinic,
    Patient,
    Payment,
    PaymentStatus,
    RoleName,
    ServiceItem,
    User,
)
from app.schemas.billing import BillCreate, BillItemUpdate, BillLineInput, BillUpdate, PaymentInput, PaymentUpdate
from app.services import audit_service, patient_service
from app.utils.exceptions import DuplicateResourceError, NotFoundError, ValidationError
from app.utils.identifiers import generate_bill_number

logger = logging.getLogger(__name__)

TWO_PLACES = Decimal("0.01")

_BILL_LOADS = (
    selectinload(Bill.items),
    selectinload(Bill.payments).selectinload(Payment.received_by),
    selectinload(Bill.clinic),
    selectinload(Bill.patient),
)


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(TWO_PLACES)


# --------------------------------------------------------------------------- #
# Service catalogue
# --------------------------------------------------------------------------- #
def list_service_items(
    db: Session, user: User, *, clinic_id: int | None = None, include_inactive: bool = True
) -> list[ServiceItem]:
    """Catalogue entries usable at a clinic: its own plus the chain-wide ones."""
    stmt = select(ServiceItem).order_by(ServiceItem.sort_order, ServiceItem.name)

    if clinic_id is not None:
        permissions.assert_clinic_access(db, user, clinic_id)
        stmt = stmt.where(
            or_(ServiceItem.clinic_id == clinic_id, ServiceItem.clinic_id.is_(None))
        )
    else:
        accessible = permissions.accessible_clinic_ids(db, user)
        if accessible is not None:
            stmt = stmt.where(
                or_(
                    ServiceItem.clinic_id.in_(accessible or [-1]),
                    ServiceItem.clinic_id.is_(None),
                )
            )
    if not include_inactive:
        stmt = stmt.where(ServiceItem.is_active.is_(True))

    return list(db.execute(stmt).unique().scalars().all())


def get_service_item(db: Session, service_item_id: int) -> ServiceItem:
    item = db.get(ServiceItem, service_item_id)
    if item is None:
        raise NotFoundError(f"Service {service_item_id} was not found")
    return item


def _assert_may_manage_catalogue(db: Session, user: User, clinic_id: int | None) -> None:
    """Who may change what a service costs.

    A chain-wide entry sets the price every branch quotes, so it belongs to
    Superadmin/Admin. A clinic-scoped entry is that branch's own business and
    only needs access to the branch.
    """
    if clinic_id is None:
        permissions.require_roles(user, permissions.ALL_CLINIC_ROLES)
    else:
        permissions.assert_clinic_access(db, user, clinic_id)


def create_service_item(db: Session, user: User, payload, request: Request | None = None):
    _assert_may_manage_catalogue(db, user, payload.clinic_id)

    clash = db.execute(
        select(ServiceItem).where(
            func.lower(ServiceItem.name) == payload.name.lower(),
            ServiceItem.clinic_id.is_(None)
            if payload.clinic_id is None
            else ServiceItem.clinic_id == payload.clinic_id,
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise DuplicateResourceError(f"A service named '{payload.name}' already exists here")

    item = ServiceItem(**payload.model_dump())
    db.add(item)
    db.flush()
    audit_service.record(
        db,
        action=AuditAction.CREATED,
        user=user,
        entity_type="service_item",
        entity_id=item.id,
        clinic_id=item.clinic_id,
        description=f"Added service '{item.name}' at {item.default_price}",
        request=request,
    )
    db.commit()
    db.refresh(item)
    return item


def update_service_item(
    db: Session, user: User, service_item_id: int, payload, request: Request | None = None
):
    item = get_service_item(db, service_item_id)
    _assert_may_manage_catalogue(db, user, item.clinic_id)

    data = payload.model_dump(exclude_unset=True)
    changed = {}
    for field, value in data.items():
        if getattr(item, field) != value:
            changed[field] = str(value)
            setattr(item, field, value)

    audit_service.record(
        db,
        action=AuditAction.UPDATED,
        user=user,
        entity_type="service_item",
        entity_id=item.id,
        clinic_id=item.clinic_id,
        description=f"Updated service '{item.name}'",
        details={"changed_fields": changed} if changed else None,
        request=request,
    )
    db.commit()
    db.refresh(item)
    return item


# --------------------------------------------------------------------------- #
# Bills
# --------------------------------------------------------------------------- #
def _resolve_clinic(db: Session, user: User, patient: Patient, clinic_id: int | None) -> int:
    if clinic_id is not None:
        permissions.assert_clinic_access(db, user, clinic_id)
        return clinic_id
    if user.role_name == RoleName.CLINIC_USER and user.primary_clinic_id:
        return user.primary_clinic_id
    if patient.primary_clinic_id:
        return patient.primary_clinic_id
    raise ValidationError("No clinic could be determined for this bill; pass clinic_id")


def _apply_payment_status(bill: Bill) -> None:
    """Derive the payment status from the numbers, never set it by hand."""
    if bill.amount_paid <= 0:
        bill.payment_status = PaymentStatus.UNPAID
    elif bill.amount_paid >= bill.total_amount:
        bill.payment_status = PaymentStatus.PAID
    else:
        bill.payment_status = PaymentStatus.PARTIAL


def build_bill(
    db: Session,
    user: User,
    patient: Patient,
    *,
    clinic_id: int,
    lines: list[BillLineInput],
    bill_date: date | None = None,
    discount_amount: Decimal = Decimal("0.00"),
    tax_amount: Decimal = Decimal("0.00"),
    payment: PaymentInput | None = None,
    package_id: int | None = None,
    notes: str | None = None,
) -> Bill:
    """Create a finalised bill with its lines and optional payment.

    Does **not** commit: the caller owns the transaction, so registering a
    package and billing for it either both happen or neither does.
    """
    if not lines:
        raise ValidationError("A bill needs at least one charge")

    subtotal = sum((line.amount for line in lines), Decimal("0.00")).quantize(TWO_PLACES)
    discount = money(discount_amount)
    tax = money(tax_amount)
    if discount > subtotal:
        raise ValidationError("Discount cannot be greater than the subtotal")
    total = (subtotal - discount + tax).quantize(TWO_PLACES)

    if payment is not None and payment.total_amount > total:
        raise ValidationError(
            f"Payment of {payment.total_amount} is more than the bill total of {total}. "
            "Reduce the amount, or add the extra as a separate charge."
        )

    clinic = db.get(Clinic, clinic_id)
    bill = Bill(
        # Each separately registered clinic keeps its own invoice series.
        bill_number=generate_bill_number(
            db,
            bill_date or local_today(),
            series=clinic.bill_number_prefix if clinic else None,
        ),
        clinic_id=clinic_id,
        patient_id=patient.id,
        package_id=package_id,
        bill_date=bill_date or local_today(),
        subtotal_amount=subtotal,
        discount_amount=discount,
        tax_amount=tax,
        total_amount=total,
        amount_paid=Decimal("0.00"),
        # Each bill here is a receipt for a transaction that has happened, so it
        # is finalised on creation rather than left as an editable draft.
        status=BillStatus.FINALIZED,
        notes=notes,
        created_by_user_id=user.id,
    )
    db.add(bill)
    db.flush()

    for line in lines:
        db.add(
            BillItem(
                bill_id=bill.id,
                item_type=line.item_type,
                service_item_id=line.service_item_id,
                # Copied, not referenced: renaming a service later must not
                # rewrite what this bill said on the day it was issued.
                description=line.description,
                quantity=line.quantity,
                unit_price=money(line.unit_price),
                amount=line.amount,
            )
        )

    if payment is not None:
        from app.models.enums import PaymentMethod as PM
        from app.services import physio_points_service
        paid = Decimal("0.00")
        initial_payments: list[Payment] = []
        for split in payment.splits:
            p = Payment(
                bill_id=bill.id,
                amount=money(split.amount),
                payment_method=split.payment_method,
                payment_date=payment.payment_date or bill.bill_date,
                reference_number=split.reference_number,
                received_by_user_id=user.id,
                notes=payment.notes,
            )
            db.add(p)
            initial_payments.append(p)
            paid += money(split.amount)
        db.flush()  # get payment IDs
        for p in initial_payments:
            if p.payment_method == PM.POINTS:
                physio_points_service.redeem_from_payment(db, p, bill, user)
            else:
                physio_points_service.earn_from_payment(db, p, bill, user)
        bill.amount_paid = paid

    _apply_payment_status(bill)
    db.flush()
    return bill


def create_bill(
    db: Session,
    user: User,
    patient_id: int,
    payload: BillCreate,
    request: Request | None = None,
) -> Bill:
    """An ad-hoc charge: consultation, a laser session, a product."""
    patient = patient_service.get_patient(db, user, patient_id)
    clinic_id = _resolve_clinic(db, user, patient, payload.clinic_id)

    bill = build_bill(
        db,
        user,
        patient,
        clinic_id=clinic_id,
        lines=payload.items,
        bill_date=payload.bill_date,
        discount_amount=payload.discount_amount,
        tax_amount=payload.tax_amount,
        payment=payload.payment,
        package_id=payload.package_id,
        notes=payload.notes,
    )

    audit_service.record(
        db,
        action=AuditAction.BILL_GENERATED,
        user=user,
        entity_type="bill",
        entity_id=bill.id,
        clinic_id=clinic_id,
        description=(
            f"Bill {bill.bill_number} for {patient.patient_code}: {bill.total_amount} "
            f"({len(payload.items)} item(s)), paid {bill.amount_paid}"
        ),
        request=request,
    )
    db.commit()
    db.refresh(bill)
    return bill


def add_payment(
    db: Session,
    user: User,
    bill_id: int,
    payload: PaymentInput,
    request: Request | None = None,
) -> Bill:
    """Record money received against an existing bill."""
    bill = get_bill(db, user, bill_id)
    if bill.status == BillStatus.CANCELLED:
        raise ValidationError("This bill has been cancelled")

    total_amount = payload.total_amount
    outstanding = bill.balance_amount
    if outstanding <= 0:
        raise ValidationError(f"Bill {bill.bill_number} is already fully paid")
    if total_amount > outstanding:
        raise ValidationError(
            f"That is more than the {outstanding} outstanding on {bill.bill_number}"
        )

    pay_date = payload.payment_date or local_today()

    # Validate POINTS splits before touching the DB
    from app.models.enums import PaymentMethod as PM
    from app.services import physio_points_service
    points_splits_total = sum(
        s.amount for s in payload.splits if s.payment_method == PM.POINTS
    )
    if points_splits_total > 0:
        from app.models.patient import Patient as _Patient
        _patient = db.get(_Patient, bill.patient_id)
        available = _patient.physio_points if _patient else 0
        if int(points_splits_total) > available:
            raise ValidationError(
                f"Cannot redeem {int(points_splits_total)} Physio Points — only {available} available"
            )

    new_payments: list[Payment] = []
    for split in payload.splits:
        p = Payment(
            bill_id=bill.id,
            amount=money(split.amount),
            payment_method=split.payment_method,
            payment_date=pay_date,
            reference_number=split.reference_number,
            received_by_user_id=user.id,
            notes=payload.notes,
        )
        db.add(p)
        new_payments.append(p)
    db.flush()  # get IDs for points ledger

    for p in new_payments:
        if p.payment_method == PM.POINTS:
            physio_points_service.redeem_from_payment(db, p, bill, user)
        else:
            physio_points_service.earn_from_payment(db, p, bill, user)

    bill.amount_paid = money(bill.amount_paid + total_amount)
    _apply_payment_status(bill)

    split_desc = " + ".join(
        f"{money(s.amount)} {s.payment_method.value}"
        + (f" (ref {s.reference_number})" if s.reference_number else "")
        for s in payload.splits
    )
    audit_service.record(
        db,
        action=AuditAction.PAYMENT_RECORDED,
        user=user,
        entity_type="bill",
        entity_id=bill.id,
        clinic_id=bill.clinic_id,
        description=f"{total_amount} received on {bill.bill_number}: {split_desc}",
        request=request,
    )
    db.commit()
    db.refresh(bill)
    return bill


def update_bill(
    db: Session,
    user: User,
    bill_id: int,
    payload: BillUpdate,
    request: Request | None = None,
) -> Bill:
    """Correct bill_date, notes, line items or discount after a data-entry mistake."""
    bill = get_bill(db, user, bill_id)
    if bill.status == BillStatus.CANCELLED:
        raise ValidationError("A cancelled bill cannot be edited")

    changed: list[str] = []

    # --- scalar fields ---
    if payload.bill_date is not None:
        bill.bill_date = payload.bill_date
        changed.append("bill_date")
    if payload.notes is not None:
        bill.notes = payload.notes or None
        changed.append("notes")

    # --- line items ---
    if payload.items is not None:
        item_map: dict[int, BillItemUpdate] = {u.id: u for u in payload.items}
        for bill_item in bill.items:
            if bill_item.id in item_map:
                upd = item_map[bill_item.id]
                bill_item.description = upd.description
                bill_item.quantity = upd.quantity
                bill_item.unit_price = upd.unit_price
                bill_item.amount = upd.amount
        changed.append("items")

    # --- recalculate totals when items or discount changed ---
    if payload.items is not None or payload.discount_amount is not None:
        subtotal = sum((item.amount for item in bill.items), Decimal("0.00")).quantize(TWO_PLACES)
        discount = money(payload.discount_amount if payload.discount_amount is not None else bill.discount_amount)
        if discount > subtotal:
            raise ValidationError("Discount cannot be greater than the subtotal")
        total = (subtotal - discount + bill.tax_amount).quantize(TWO_PLACES)
        if total < bill.amount_paid:
            raise ValidationError(
                f"New total ({total}) cannot be less than the amount already paid ({bill.amount_paid}). "
                "Reduce the payment first, or keep the total at or above what has been received."
            )
        bill.subtotal_amount = subtotal
        bill.discount_amount = discount
        bill.total_amount = total
        _apply_payment_status(bill)
        if payload.discount_amount is not None:
            changed.append("discount")
        changed.append("totals")

    if not changed:
        return bill

    audit_service.record(
        db,
        action=AuditAction.UPDATED,
        user=user,
        entity_type="bill",
        entity_id=bill.id,
        clinic_id=bill.clinic_id,
        description=f"Corrected {', '.join(changed)} on {bill.bill_number}",
        request=request,
    )
    db.commit()
    db.refresh(bill)
    return bill


def update_payment(
    db: Session,
    user: User,
    payment_id: int,
    payload: PaymentUpdate,
    request: Request | None = None,
) -> Bill:
    """Correct date, method or reference on a recorded payment."""
    payment = db.get(Payment, payment_id)
    if payment is None:
        raise NotFoundError(f"Payment {payment_id} was not found")
    bill = get_bill(db, user, payment.bill_id)

    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(payment, field, value)

    # If the amount was corrected, recalculate the bill's running total.
    if "amount" in data:
        new_total = money(sum(p.amount for p in bill.payments))
        if new_total > bill.total_amount:
            raise ValidationError(
                f"Corrected payment total ₹{new_total} would exceed "
                f"bill total ₹{bill.total_amount}"
            )
        bill.amount_paid = new_total
        _apply_payment_status(bill)

    audit_service.record(
        db,
        action=AuditAction.UPDATED,
        user=user,
        entity_type="payment",
        entity_id=payment_id,
        clinic_id=bill.clinic_id,
        description=f"Corrected {', '.join(data)} on payment {payment_id} ({bill.bill_number})",
        request=request,
    )
    db.commit()
    db.refresh(bill)
    return bill


def get_bill(db: Session, user: User, bill_id: int) -> Bill:
    bill = db.execute(
        select(Bill).options(*_BILL_LOADS).where(Bill.id == bill_id)
    ).unique().scalar_one_or_none()
    if bill is None:
        raise NotFoundError(f"Bill {bill_id} was not found")
    permissions.assert_clinic_access(db, user, bill.clinic_id)
    return bill


def list_bills(
    db: Session,
    user: User,
    *,
    patient_id: int | None = None,
    clinic_id: int | None = None,
    payment_status: PaymentStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    payment_date_from: date | None = None,
    payment_date_to: date | None = None,
    search: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Bill], int, dict[int, Decimal]]:
    """Return (bills, total_count, period_paid_map).

    period_paid_map maps bill_id → amount paid within the payment date range.
    It is empty when no payment date filter is active.
    """
    stmt = select(Bill).options(*_BILL_LOADS)

    accessible = permissions.accessible_clinic_ids(db, user)
    if accessible is not None:
        stmt = stmt.where(Bill.clinic_id.in_(accessible or [-1]))
    if clinic_id is not None:
        permissions.assert_clinic_access(db, user, clinic_id)
        stmt = stmt.where(Bill.clinic_id == clinic_id)
    if patient_id is not None:
        patient_service.get_patient(db, user, patient_id)
        stmt = stmt.where(Bill.patient_id == patient_id)
    if payment_status is not None:
        stmt = stmt.where(Bill.payment_status == payment_status)

    payment_date_filter = payment_date_from is not None or payment_date_to is not None
    if payment_date_filter:
        # Filter bills that have at least one payment in the requested period.
        period_subq = select(Payment.bill_id).distinct()
        if payment_date_from is not None:
            period_subq = period_subq.where(Payment.payment_date >= payment_date_from)
        if payment_date_to is not None:
            period_subq = period_subq.where(Payment.payment_date <= payment_date_to)
        stmt = stmt.where(Bill.id.in_(period_subq))
    else:
        if date_from is not None:
            stmt = stmt.where(Bill.bill_date >= date_from)
        if date_to is not None:
            stmt = stmt.where(Bill.bill_date <= date_to)

    if search:
        pattern = f"%{search.strip().lower()}%"
        stmt = stmt.join(Patient, Bill.patient_id == Patient.id).where(
            or_(
                func.lower(Patient.full_name).like(pattern),
                func.lower(Patient.patient_code).like(pattern),
                func.lower(Bill.bill_number).like(pattern),
            )
        )

    total = db.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar_one()
    rows = (
        db.execute(stmt.order_by(Bill.bill_date.desc(), Bill.id.desc()).offset(offset).limit(limit))
        .unique()
        .scalars()
        .all()
    )
    bills = list(rows)

    # Build period_paid_map: amount paid per bill within the payment date range.
    period_map: dict[int, Decimal] = {}
    if payment_date_filter and bills:
        pay_stmt = (
            select(Payment.bill_id, func.sum(Payment.amount))
            .where(Payment.bill_id.in_([b.id for b in bills]))
            .group_by(Payment.bill_id)
        )
        if payment_date_from is not None:
            pay_stmt = pay_stmt.where(Payment.payment_date >= payment_date_from)
        if payment_date_to is not None:
            pay_stmt = pay_stmt.where(Payment.payment_date <= payment_date_to)
        period_map = {bill_id: money(amt) for bill_id, amt in db.execute(pay_stmt).all()}

    return bills, total, period_map


def billing_counters(db: Session, user: User, clinic_id: int | None = None) -> dict:
    """Collected and outstanding, for the billing header."""
    stmt = select(
        func.coalesce(func.sum(Bill.total_amount), 0),
        func.coalesce(func.sum(Bill.amount_paid), 0),
        func.count(),
    ).where(Bill.status != BillStatus.CANCELLED)

    accessible = permissions.accessible_clinic_ids(db, user)
    if accessible is not None:
        stmt = stmt.where(Bill.clinic_id.in_(accessible or [-1]))
    if clinic_id is not None:
        stmt = stmt.where(Bill.clinic_id == clinic_id)

    billed, collected, count = db.execute(stmt).one()
    today_collected = db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0))
        .select_from(Payment)
        .join(Bill, Payment.bill_id == Bill.id)
        .where(
            Payment.payment_date == local_today(),
            Bill.status != BillStatus.CANCELLED,
            *([Bill.clinic_id.in_(accessible or [-1])] if accessible is not None else []),
            *([Bill.clinic_id == clinic_id] if clinic_id is not None else []),
        )
    ).scalar_one()

    return {
        "bills": count,
        "total_billed": money(billed),
        "total_collected": money(collected),
        "outstanding": max(money(billed) - money(collected), Decimal("0.00")),
        "collected_today": money(today_collected),
    }


def payment_mode_breakdown(
    db: Session,
    user: User,
    clinic_id: int | None,
    date_from: date | None,
    date_to: date | None,
) -> dict:
    """Amount collected per payment method (CASH / UPI / CARD) for a date range."""
    accessible = permissions.accessible_clinic_ids(db, user)
    stmt = (
        select(Payment.payment_method, func.coalesce(func.sum(Payment.amount), 0))
        .select_from(Payment)
        .join(Bill, Payment.bill_id == Bill.id)
        .where(Bill.status != BillStatus.CANCELLED)
        .group_by(Payment.payment_method)
    )
    if accessible is not None:
        stmt = stmt.where(Bill.clinic_id.in_(accessible or [-1]))
    if clinic_id is not None:
        stmt = stmt.where(Bill.clinic_id == clinic_id)
    if date_from is not None:
        stmt = stmt.where(Payment.payment_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(Payment.payment_date <= date_to)

    result: dict = {"cash": Decimal("0.00"), "upi": Decimal("0.00"), "card": Decimal("0.00"), "total": Decimal("0.00")}
    for method, amount in db.execute(stmt).all():
        key = method.lower() if isinstance(method, str) else method.value.lower()
        if key in result:
            result[key] = money(amount)
        result["total"] = money(result["total"] + money(amount))
    return result


def package_line(
    sessions: int, price_per_session: Decimal, package_name: str | None = None
) -> BillLineInput:
    """The bill line for a treatment package.

    A custom package name is *appended to* the session count rather than
    replacing it. Using the name alone let a package called "21" produce an
    invoice line reading just "21" -- meaningless to the patient reading it, and
    it lands in the per-service revenue report as its own nonsense category.
    The count is the part that must always be there.
    """
    default = f"{sessions}-session physiotherapy package"
    name = (package_name or "").strip()
    if not name or name == default:
        description = default
    else:
        description = f"{name} ({sessions} sessions)"
    return BillLineInput(
        description=description,
        unit_price=money(price_per_session),
        quantity=sessions,
        item_type=BillItemType.SESSION_PACKAGE,
    )
