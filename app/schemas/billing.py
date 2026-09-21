"""Billing schemas (Sections 17-19).

The shape that makes the awkward cases work is simple: **a bill is a list of
line items, and payments are recorded against the bill.** A treatment package is
one line among others, so "one session plus a consultation fee" and "a 10-session
course plus a laser therapy add-on" need no special handling -- they are just
different lines.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import BillItemType, BillStatus, PaymentMethod, PaymentStatus
from app.schemas.common import ORMModel, clean_text

TWO_PLACES = Decimal("0.01")


# --------------------------------------------------------------------------- #
# Service catalogue
# --------------------------------------------------------------------------- #
class ServiceItemBase(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    item_type: BillItemType = BillItemType.OTHER
    default_price: Decimal = Field(default=Decimal("0.00"), ge=0, le=1_000_000)
    description: str | None = None
    #: NULL means the service is offered at every clinic.
    clinic_id: int | None = None
    is_active: bool = True
    sort_order: int = Field(default=0, ge=0, le=999)

    @field_validator("name", "description")
    @classmethod
    def _clean(cls, value):
        return clean_text(value)


class ServiceItemCreate(ServiceItemBase):
    pass


class ServiceItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=120)
    item_type: BillItemType | None = None
    default_price: Decimal | None = Field(default=None, ge=0, le=1_000_000)
    description: str | None = None
    is_active: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=999)

    @field_validator("name", "description")
    @classmethod
    def _clean(cls, value):
        return clean_text(value) if value is not None else None


class ServiceItemRead(ORMModel, ServiceItemBase):
    id: int
    clinic_name: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, value):
        if not hasattr(value, "default_price"):
            return value
        return {
            "id": value.id,
            "name": value.name,
            "item_type": value.item_type,
            "default_price": value.default_price,
            "description": value.description,
            "clinic_id": value.clinic_id,
            "clinic_name": value.clinic.name if value.clinic else None,
            "is_active": value.is_active,
            "sort_order": value.sort_order,
        }


# --------------------------------------------------------------------------- #
# Bill inputs
# --------------------------------------------------------------------------- #
class BillLineInput(BaseModel):
    """One chargeable line.

    `service_item_id` links to the catalogue when the line came from it; the
    description and price are still stored on the line, so renaming or repricing
    a service later never rewrites a historical bill.
    """

    description: str = Field(min_length=1, max_length=255)
    unit_price: Decimal = Field(ge=0, le=1_000_000)
    quantity: int = Field(default=1, ge=1, le=1000)
    item_type: BillItemType = BillItemType.OTHER
    service_item_id: int | None = None

    @field_validator("description")
    @classmethod
    def _clean(cls, value):
        cleaned = clean_text(value)
        if not cleaned:
            raise ValueError("Each charge needs a description")
        return cleaned

    @property
    def amount(self) -> Decimal:
        return (self.unit_price * self.quantity).quantize(TWO_PLACES)


class PaymentInput(BaseModel):
    """Money actually received. `reference_number` is the UPI/card/transaction id."""

    amount: Decimal = Field(gt=0, le=10_000_000)
    payment_method: PaymentMethod
    reference_number: str | None = Field(default=None, max_length=100)
    payment_date: date | None = None
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("reference_number", "notes")
    @classmethod
    def _clean(cls, value):
        return clean_text(value)


class BillCreate(BaseModel):
    """An ad-hoc charge: a consultation, a laser session, a product."""

    items: list[BillLineInput] = Field(min_length=1)
    clinic_id: int | None = None
    bill_date: date | None = None
    discount_amount: Decimal = Field(default=Decimal("0.00"), ge=0)
    tax_amount: Decimal = Field(default=Decimal("0.00"), ge=0)
    payment: PaymentInput | None = None
    notes: str | None = None
    package_id: int | None = None

    @field_validator("notes")
    @classmethod
    def _clean(cls, value):
        return clean_text(value)

    @model_validator(mode="after")
    def _check_discount(self):
        subtotal = sum((line.amount for line in self.items), Decimal("0.00"))
        if self.discount_amount > subtotal:
            raise ValueError("Discount cannot be greater than the subtotal")
        return self


# --------------------------------------------------------------------------- #
# Bill outputs
# --------------------------------------------------------------------------- #
class BillItemRead(ORMModel):
    id: int
    item_type: BillItemType
    service_item_id: int | None = None
    description: str
    quantity: int
    unit_price: Decimal
    amount: Decimal


class PaymentRead(ORMModel):
    id: int
    amount: Decimal
    payment_method: PaymentMethod
    payment_date: date
    reference_number: str | None = None
    received_by: str | None = None
    notes: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, value):
        if not hasattr(value, "payment_method"):
            return value
        return {
            "id": value.id,
            "amount": value.amount,
            "payment_method": value.payment_method,
            "payment_date": value.payment_date,
            "reference_number": value.reference_number,
            "received_by": value.received_by.full_name if value.received_by else None,
            "notes": value.notes,
        }


class BillRead(ORMModel):
    id: int
    bill_number: str
    clinic_id: int
    clinic_name: str | None = None
    patient_id: int
    patient_name: str | None = None
    patient_code: str | None = None
    package_id: int | None = None
    bill_date: date
    subtotal_amount: Decimal
    discount_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    amount_paid: Decimal
    balance_amount: Decimal
    status: BillStatus
    payment_status: PaymentStatus
    notes: str | None = None
    created_at: datetime
    items: list[BillItemRead] = Field(default_factory=list)
    payments: list[PaymentRead] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, value):
        if not hasattr(value, "bill_number"):
            return value
        return {
            "id": value.id,
            "bill_number": value.bill_number,
            "clinic_id": value.clinic_id,
            "clinic_name": value.clinic.name if value.clinic else None,
            "patient_id": value.patient_id,
            "patient_name": value.patient.full_name if value.patient else None,
            "patient_code": value.patient.patient_code if value.patient else None,
            "package_id": value.package_id,
            "bill_date": value.bill_date,
            "subtotal_amount": value.subtotal_amount,
            "discount_amount": value.discount_amount,
            "tax_amount": value.tax_amount,
            "total_amount": value.total_amount,
            "amount_paid": value.amount_paid,
            "balance_amount": value.balance_amount,
            "status": value.status,
            "payment_status": value.payment_status,
            "notes": value.notes,
            "created_at": value.created_at,
            "items": list(value.items),
            "payments": list(value.payments),
        }


class BillCounters(BaseModel):
    """Header tallies.

    Declared as a model rather than returned as a bare dict so the `Decimal`
    fields serialise as JSON *strings*. A bare dict goes through
    `jsonable_encoder`, which turns them into floats -- reintroducing at the
    edge exactly the binary-fraction error the whole money path avoids.
    """

    bills: int
    total_billed: Decimal
    total_collected: Decimal
    outstanding: Decimal
    collected_today: Decimal


class BillSummaryRow(ORMModel):
    """Compact row for the bills list."""

    id: int
    bill_number: str
    patient_id: int
    patient_name: str | None = None
    patient_code: str | None = None
    clinic_name: str | None = None
    bill_date: date
    total_amount: Decimal
    amount_paid: Decimal
    balance_amount: Decimal
    payment_status: PaymentStatus
    status: BillStatus

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, value):
        if not hasattr(value, "bill_number"):
            return value
        return {
            "id": value.id,
            "bill_number": value.bill_number,
            "patient_id": value.patient_id,
            "patient_name": value.patient.full_name if value.patient else None,
            "patient_code": value.patient.patient_code if value.patient else None,
            "clinic_name": value.clinic.name if value.clinic else None,
            "bill_date": value.bill_date,
            "total_amount": value.total_amount,
            "amount_paid": value.amount_paid,
            "balance_amount": value.balance_amount,
            "payment_status": value.payment_status,
            "status": value.status,
        }
