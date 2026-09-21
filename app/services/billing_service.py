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
from app.schemas.billing import BillCreate, BillLineInput, PaymentInput
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

    if payment is not None and money(payment.amount) > total:
        raise ValidationError(
            f"Payment of {money(payment.amount)} is more than the bill total of {total}. "
            "Reduce the amount, or add the extra as a separate charge."
        )

    clinic = db.get(Clinic, clinic_id)
    bill = Bill(
        # Each separately registered clinic keeps its own invoice series.
        bill_number=generate_bill_number(
            db,
            bill_date or date.today(),
            series=clinic.bill_number_prefix if clinic else None,
        ),
        clinic_id=clinic_id,
        patient_id=patient.id,
        package_id=package_id,
        bill_date=bill_date or date.today(),
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
        db.add(
            Payment(
                bill_id=bill.id,
                amount=money(payment.amount),
                payment_method=payment.payment_method,
                payment_date=payment.payment_date or bill.bill_date,
                reference_number=payment.reference_number,
                received_by_user_id=user.id,
                notes=payment.notes,
            )
        )
        bill.amount_paid = money(payment.amount)

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

    amount = money(payload.amount)
    outstanding = bill.balance_amount
    if outstanding <= 0:
        raise ValidationError(f"Bill {bill.bill_number} is already fully paid")
    if amount > outstanding:
        raise ValidationError(
            f"That is more than the {outstanding} outstanding on {bill.bill_number}"
        )

    db.add(
        Payment(
            bill_id=bill.id,
            amount=amount,
            payment_method=payload.payment_method,
            payment_date=payload.payment_date or date.today(),
            reference_number=payload.reference_number,
            received_by_user_id=user.id,
            notes=payload.notes,
        )
    )
    bill.amount_paid = money(bill.amount_paid + amount)
    _apply_payment_status(bill)

    audit_service.record(
        db,
        action=AuditAction.PAYMENT_RECORDED,
        user=user,
        entity_type="bill",
        entity_id=bill.id,
        clinic_id=bill.clinic_id,
        description=(
            f"{amount} received on {bill.bill_number} by "
            f"{payload.payment_method.value}"
            + (f" (ref {payload.reference_number})" if payload.reference_number else "")
        ),
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
    search: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Bill], int]:
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
    return list(rows), total


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
            Payment.payment_date == date.today(),
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
