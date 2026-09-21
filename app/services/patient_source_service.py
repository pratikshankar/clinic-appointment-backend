"""Patient-source configuration (Section 11).

Sources are deactivated rather than deleted: patients already attributed to a
source must keep that attribution, or historical acquisition reports (Phase 8)
would quietly change as the list is edited.
"""

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AuditAction, Patient, PatientSource, User
from app.schemas.patient_source import PatientSourceCreate, PatientSourceUpdate
from app.services import audit_service
from app.utils.exceptions import DuplicateResourceError, NotFoundError, ValidationError


def list_sources(
    db: Session, include_inactive: bool = True
) -> list[tuple[PatientSource, int]]:
    """Sources with the number of patients attributed to each."""
    stmt = (
        select(PatientSource, func.count(Patient.id))
        .outerjoin(Patient, Patient.source_id == PatientSource.id)
        .group_by(PatientSource.id)
        .order_by(PatientSource.sort_order, PatientSource.name)
    )
    if not include_inactive:
        stmt = stmt.where(PatientSource.is_active.is_(True))
    return [(source, count) for source, count in db.execute(stmt).all()]


def get_source(db: Session, source_id: int) -> PatientSource:
    source = db.get(PatientSource, source_id)
    if source is None:
        raise NotFoundError(f"Patient source {source_id} was not found")
    return source


def _assert_name_available(db: Session, name: str, exclude_id: int | None = None) -> None:
    stmt = select(PatientSource).where(func.lower(PatientSource.name) == name.lower())
    if exclude_id is not None:
        stmt = stmt.where(PatientSource.id != exclude_id)
    if db.execute(stmt).scalar_one_or_none() is not None:
        raise DuplicateResourceError(f"A patient source named '{name}' already exists")


def create_source(
    db: Session, payload: PatientSourceCreate, actor: User, request: Request | None = None
) -> PatientSource:
    _assert_name_available(db, payload.name)
    source = PatientSource(
        name=payload.name, is_active=payload.is_active, sort_order=payload.sort_order
    )
    db.add(source)
    db.flush()
    audit_service.record(
        db,
        action=AuditAction.CREATED,
        user=actor,
        entity_type="patient_source",
        entity_id=source.id,
        description=f"Created patient source '{source.name}'",
        request=request,
    )
    db.commit()
    db.refresh(source)
    return source


def update_source(
    db: Session,
    source_id: int,
    payload: PatientSourceUpdate,
    actor: User,
    request: Request | None = None,
) -> PatientSource:
    source = get_source(db, source_id)
    data = payload.model_dump(exclude_unset=True)

    if "name" in data and data["name"]:
        _assert_name_available(db, data["name"], exclude_id=source_id)

    if data.get("is_active") is False:
        remaining = db.execute(
            select(func.count())
            .select_from(PatientSource)
            .where(PatientSource.is_active.is_(True), PatientSource.id != source_id)
        ).scalar_one()
        if remaining == 0:
            raise ValidationError(
                "At least one patient source must stay active, otherwise new patients "
                "cannot record where they came from"
            )

    changed = {}
    for field, value in data.items():
        if getattr(source, field) != value:
            changed[field] = value
            setattr(source, field, value)

    audit_service.record(
        db,
        action=AuditAction.UPDATED,
        user=actor,
        entity_type="patient_source",
        entity_id=source.id,
        description=f"Updated patient source '{source.name}'",
        details={"changed_fields": changed} if changed else None,
        request=request,
    )
    db.commit()
    db.refresh(source)
    return source


def patient_count(db: Session, source_id: int) -> int:
    return db.execute(
        select(func.count()).select_from(Patient).where(Patient.source_id == source_id)
    ).scalar_one()
