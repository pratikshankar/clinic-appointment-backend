"""Configurable catalogue of chargeable services (consultation, laser therapy…).

Why a catalogue rather than free text: staff otherwise retype "Laser therapy"
and its price on every bill, prices drift between people and clinics, and
Phase 8 cannot report revenue by service without guessing at spelling variants.

A catalogue entry is a *default*, not a rule -- the price stays editable on the
bill, because a clinic occasionally discounts or a service is quoted specially.
"""

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.models.enums import BillItemType
from app.models.mixins import TimestampMixin, enum_column

if TYPE_CHECKING:  # pragma: no cover
    from app.models.clinic import Clinic


class ServiceItem(Base, TimestampMixin):
    __tablename__ = "service_items"
    __table_args__ = (
        # Chain-wide entries (clinic_id NULL) and per-clinic overrides can share
        # a name; two entries with the same name at the same scope cannot.
        UniqueConstraint("clinic_id", "name", name="uq_service_items_clinic_name"),
        CheckConstraint("default_price >= 0", name="ck_service_items_price_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: NULL means the service is offered at every clinic in the chain.
    clinic_id: Mapped[int | None] = mapped_column(
        ForeignKey("clinics.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    item_type: Mapped[BillItemType] = enum_column(
        BillItemType, default=BillItemType.OTHER, nullable=False
    )
    default_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    clinic: Mapped["Clinic | None"] = relationship(lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ServiceItem {self.name} {self.default_price}>"
