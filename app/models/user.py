"""Users, roles and the clinic assignment association."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.models.enums import RoleName
from app.models.mixins import TimestampMixin, enum_column, UTCDateTime

if TYPE_CHECKING:  # pragma: no cover
    from app.models.clinic import Clinic


class Role(Base, TimestampMixin):
    """Role lookup table.

    The role *set* is fixed by the business rules (Section 23), but keeping it
    as a table rather than a bare column means permissions metadata and future
    custom roles have a home.
    """

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[RoleName] = enum_column(RoleName, unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(255))

    users: Mapped[list["User"]] = relationship(back_populates="role")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Role {self.name}>"


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20))
    #: The clinic's own staff number. Unique so it can be used to identify a
    #: person in payroll or rota systems, but optional -- a chain that does not
    #: use employee numbers should not be forced to invent them.
    employee_id: Mapped[str | None] = mapped_column(String(40), unique=True, index=True)
    #: A physiotherapist's professional registration number. Printed on the
    #: treatment statement beside their name, because that is the line an
    #: insurer looks for when deciding whether a claim is from a qualified
    #: practitioner.
    registration_number: Mapped[str | None] = mapped_column(String(60))
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime(timezone=True))

    role: Mapped[Role] = relationship(back_populates="users", lazy="joined")
    # `clinic_users` has two FKs to `users` (the assignee and whoever assigned
    # them), so the join column has to be stated explicitly.
    clinic_links: Mapped[list["ClinicUser"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
        foreign_keys="ClinicUser.user_id",
    )

    # --- convenience helpers used by the permission layer -------------------
    @property
    def role_name(self) -> RoleName:
        return self.role.name

    @property
    def clinic_ids(self) -> list[int]:
        """Clinics this user is explicitly assigned to.

        Empty for Superadmin/Admin, who are scoped by role instead (Section 38).
        """
        return [link.clinic_id for link in self.clinic_links]

    @property
    def primary_clinic_id(self) -> int | None:
        for link in self.clinic_links:
            if link.is_primary:
                return link.clinic_id
        return self.clinic_ids[0] if self.clinic_links else None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.username} ({self.role_id})>"


class ClinicUser(Base, TimestampMixin):
    """Assignment of a user to a clinic.

    Modelled as a many-to-many association even though a Clinic User currently
    belongs to exactly one clinic. This costs nothing now and avoids a schema
    migration when a therapist has to cover two locations.
    """

    __tablename__ = "clinic_users"
    __table_args__ = (UniqueConstraint("user_id", "clinic_id", name="uq_clinic_users_user_clinic"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    clinic_id: Mapped[int] = mapped_column(
        ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False, index=True
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    designation: Mapped[str | None] = mapped_column(String(100))
    assigned_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    user: Mapped[User] = relationship(back_populates="clinic_links", foreign_keys=[user_id])
    clinic: Mapped["Clinic"] = relationship(back_populates="user_links", lazy="joined")
