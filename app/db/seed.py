"""Development seed data (Section 34).

Idempotent: running it twice will not duplicate anything. Passwords come from
`CLINIC_SEED_*` environment variables, never from literals committed to source.

Usage:
    python -m app.db.seed            # create/refresh development data
    python -m app.db.seed --reset    # drop everything first, then seed
"""

import argparse
import logging
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.security import hash_password
from app.config import settings
from app.db.database import Base, SessionLocal, engine
from app.models import (
    Appointment,
    AppointmentAction,
    AppointmentHistory,
    AppointmentStatus,
    Bill,
    BillItem,
    BillItemType,
    BillStatus,
    Clinic,
    ClinicBreak,
    ClinicStatus,
    ClinicUser,
    Gender,
    Notification,
    NotificationType,
    Patient,
    PatientSession,
    PackageStatus,
    PatientSource,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Role,
    RoleName,
    ServiceItem,
    TreatmentPackage,
    User,
)
from app.schemas.clinic import ClinicCreate
from app.services import clinic_service, notification_service
from app.utils.identifiers import generate_appointment_code, generate_patient_code

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
logger = logging.getLogger("seed")

ROLE_DESCRIPTIONS = {
    RoleName.SUPERADMIN: "Full system control: users, clinics and configuration",
    RoleName.ADMIN: "Operational access to all clinics; no system administration",
    RoleName.CLINIC_USER: "Access limited to their assigned clinic",
}

PATIENT_SOURCES = [
    "Google",
    "Google Ads",
    "Instagram",
    "Facebook",
    "Doctor Referral",
    "Patient Referral",
    "Apartment",
    "Corporate",
    "Walk-in",
    "Other",
]


# --------------------------------------------------------------------------- #
# Building blocks
# --------------------------------------------------------------------------- #
def seed_roles(db: Session) -> dict[RoleName, Role]:
    roles: dict[RoleName, Role] = {}
    for name in RoleName:
        role = db.execute(select(Role).where(Role.name == name)).scalar_one_or_none()
        if role is None:
            role = Role(name=name, description=ROLE_DESCRIPTIONS[name])
            db.add(role)
            logger.info("Created role %s", name.value)
        roles[name] = role
    db.flush()
    return roles


def seed_patient_sources(db: Session) -> dict[str, PatientSource]:
    sources: dict[str, PatientSource] = {}
    for index, name in enumerate(PATIENT_SOURCES):
        source = db.execute(
            select(PatientSource).where(PatientSource.name == name)
        ).scalar_one_or_none()
        if source is None:
            source = PatientSource(name=name, sort_order=index)
            db.add(source)
        sources[name] = source
    db.flush()
    logger.info("Patient sources: %d configured", len(sources))
    return sources


#: A starter catalogue so the "Add a service" dropdown is useful on day one.
#: All chain-wide (clinic_id NULL); a branch can add its own in Settings.
SERVICE_CATALOGUE = [
    ("Initial consultation", BillItemType.CONSULTATION, Decimal("500.00")),
    ("Follow-up consultation", BillItemType.CONSULTATION, Decimal("300.00")),
    ("Laser therapy (single)", BillItemType.SESSION_PACKAGE, Decimal("800.00")),
    ("Dry needling (single)", BillItemType.SESSION_PACKAGE, Decimal("700.00")),
    ("Shockwave therapy (single)", BillItemType.SESSION_PACKAGE, Decimal("1200.00")),
    ("Home visit surcharge", BillItemType.OTHER, Decimal("400.00")),
    ("Knee support brace", BillItemType.PRODUCT, Decimal("950.00")),
    ("Resistance band set", BillItemType.PRODUCT, Decimal("450.00")),
]


def seed_service_items(db: Session) -> list[ServiceItem]:
    items: list[ServiceItem] = []
    for index, (name, item_type, price) in enumerate(SERVICE_CATALOGUE):
        item = db.execute(
            select(ServiceItem).where(
                ServiceItem.name == name, ServiceItem.clinic_id.is_(None)
            )
        ).scalar_one_or_none()
        if item is None:
            item = ServiceItem(
                name=name, item_type=item_type, default_price=price, sort_order=index
            )
            db.add(item)
        items.append(item)
    db.flush()
    logger.info("Service catalogue: %d entries", len(items))
    return items


def _get_or_create_user(
    db: Session,
    *,
    username: str,
    password: str,
    full_name: str,
    role: Role,
    email: str | None = None,
    phone: str | None = None,
) -> User:
    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if user:
        return user
    user = User(
        username=username,
        email=email,
        full_name=full_name,
        phone=phone,
        hashed_password=hash_password(password),
        role_id=role.id,
        is_active=True,
    )
    db.add(user)
    db.flush()
    logger.info("Created user %s (%s)", username, role.name.value)
    return user


def seed_clinics(db: Session) -> list[Clinic]:
    specs = [
        {
            "name": "HSR Layout",
            "address": "1st Main Road, Sector 2, HSR Layout",
            "location": "HSR Layout",
            "city": "Bengaluru",
            "state": "Karnataka",
            "pin_code": "560102",
            "phone": "9876500011",
            "email": "hsr@clinic.example.com",
        },
        {
            "name": "BTM Layout",
            "address": "16th Main Road, 2nd Stage, BTM Layout",
            "location": "BTM Layout",
            "city": "Bengaluru",
            "state": "Karnataka",
            "pin_code": "560076",
            "phone": "9876500022",
            "email": "btm@clinic.example.com",
        },
    ]

    clinics: list[Clinic] = []
    for spec in specs:
        existing = db.execute(
            select(Clinic).where(Clinic.name == spec["name"])
        ).scalar_one_or_none()
        if existing:
            clinics.append(existing)
            continue

        clinic = clinic_service.create_clinic(
            db,
            ClinicCreate(
                **spec,
                status=ClinicStatus.ACTIVE,
                slot_duration_minutes=settings.DEFAULT_SLOT_DURATION_MINUTES,
                capacity_per_slot=settings.DEFAULT_CAPACITY_PER_SLOT,
                # Monday-Saturday, 09:00-13:00 and 16:00-20:00 (Section 5).
                working_hours=[],
                breaks=[],
            ),
        )
        # A 13:00-16:00 gap already exists between shifts; this models the
        # explicit lunch break inside the morning shift.
        clinic.breaks.append(
            ClinicBreak(
                day_of_week=None,
                start_time=time(11, 30),
                end_time=time(12, 0),
                label="Tea break",
            )
        )
        db.flush()
        logger.info("Created clinic %s (%s)", clinic.name, clinic.code)
        clinics.append(clinic)
    return clinics


def seed_clinic_users(db: Session, clinics: list[Clinic], role: Role) -> list[User]:
    users: list[User] = []
    people = [
        ("hsr.reception", "Priya Nair", "9876500111", clinics[0], "Receptionist"),
        ("hsr.physio", "Dr. Arjun Menon", "9876500112", clinics[0], "Physiotherapist"),
        ("btm.reception", "Sneha Rao", "9876500221", clinics[1], "Receptionist"),
    ]
    for username, full_name, phone, clinic, designation in people:
        user = _get_or_create_user(
            db,
            username=username,
            password=settings.SEED_CLINIC_USER_PASSWORD,
            full_name=full_name,
            role=role,
            email=f"{username}@clinic.example.com",
            phone=phone,
        )
        link_exists = db.execute(
            select(ClinicUser).where(
                ClinicUser.user_id == user.id, ClinicUser.clinic_id == clinic.id
            )
        ).scalar_one_or_none()
        if not link_exists:
            db.add(
                ClinicUser(
                    user_id=user.id,
                    clinic_id=clinic.id,
                    is_primary=True,
                    designation=designation,
                )
            )
        users.append(user)
    db.flush()
    return users


def seed_patients(
    db: Session, clinics: list[Clinic], sources: dict[str, PatientSource], creator: User
) -> list[Patient]:
    specs = [
        ("Rahul Sharma", "9876543210", 34, Gender.MALE, "Lower back pain", "Lumbar strain",
         "Google", clinics[0]),
        ("Anita Desai", "9876543211", 41, Gender.FEMALE, "Frozen shoulder",
         "Adhesive capsulitis", "Instagram", clinics[0]),
        ("Vikram Iyer", "9876543212", 52, Gender.MALE, "Knee pain after surgery",
         "Post-op TKR rehab", "Doctor Referral", clinics[0]),
        ("Meera Krishnan", "9876543213", 29, Gender.FEMALE, "Neck stiffness",
         "Cervical spondylosis", "Walk-in", clinics[1]),
        ("Suresh Kumar", "9876543214", 60, Gender.MALE, "Difficulty walking",
         "Sciatica", "Patient Referral", clinics[1]),
    ]

    patients: list[Patient] = []
    for name, mobile, age, gender, complaint, diagnosis, source_name, clinic in specs:
        existing = db.execute(
            select(Patient).where(Patient.mobile == mobile, Patient.full_name == name)
        ).scalar_one_or_none()
        if existing:
            patients.append(existing)
            continue

        patient = Patient(
            patient_code=generate_patient_code(db),
            full_name=name,
            mobile=mobile,
            email=f"{name.split()[0].lower()}@example.com",
            age=age,
            gender=gender,
            address=f"{clinic.location}, {clinic.city}",
            chief_complaint=complaint,
            diagnosis=diagnosis,
            source_id=sources[source_name].id,
            registration_date=date.today() - timedelta(days=len(patients) * 3),
            primary_clinic_id=clinic.id,
            created_by_user_id=creator.id,
            is_profile_complete=True,
        )
        db.add(patient)
        db.flush()  # so the next generate_patient_code() sees this row
        patients.append(patient)
    # One deliberately incomplete profile, as an Admin booking would leave it
    # (Section 7 -> Section 11): the clinic finishes it when the patient arrives.
    incomplete = db.execute(
        select(Patient).where(Patient.mobile == "9876543215")
    ).scalar_one_or_none()
    if incomplete is None:
        incomplete = Patient(
            patient_code=generate_patient_code(db),
            full_name="Deepa Menon",
            mobile="9876543215",
            chief_complaint="Shoulder pain after fall",
            primary_clinic_id=clinics[0].id,
            registration_date=date.today(),
            created_by_user_id=creator.id,
            is_profile_complete=False,
        )
        db.add(incomplete)
        db.flush()

    logger.info("Patients: %d total (1 with an incomplete profile)", len(patients) + 1)
    return patients


def seed_packages_and_sessions(
    db: Session, patients: list[Patient], therapist: User
) -> list[TreatmentPackage]:
    plans = [
        (patients[0], 10, 4, Decimal("500.00")),
        (patients[1], 12, 7, Decimal("600.00")),
        (patients[2], 15, 2, Decimal("550.00")),
        (patients[3], 8, 8, Decimal("450.00")),
    ]

    packages: list[TreatmentPackage] = []
    for patient, registered, taken, price in plans:
        existing = db.execute(
            select(TreatmentPackage).where(TreatmentPackage.patient_id == patient.id)
        ).scalar_one_or_none()
        if existing:
            packages.append(existing)
            continue

        package = TreatmentPackage(
            patient_id=patient.id,
            clinic_id=patient.primary_clinic_id,
            package_name=f"{registered}-session physiotherapy package",
            sessions_registered=registered,
            sessions_taken=taken,
            price_per_session=price,
            start_date=patient.registration_date,
            created_by_user_id=therapist.id,
            # Status must agree with the counters, or the seeded data starts out
            # in exactly the inconsistent state Phase 5 exists to prevent.
            status=(
                PackageStatus.COMPLETED if taken >= registered else PackageStatus.ACTIVE
            ),
        )
        db.add(package)
        db.flush()

        for number in range(1, taken + 1):
            db.add(
                PatientSession(
                    patient_id=patient.id,
                    package_id=package.id,
                    clinic_id=package.clinic_id,
                    session_number=number,
                    session_date=(package.start_date or date.today())
                    + timedelta(days=2 * (number - 1)),
                    therapist_user_id=therapist.id,
                    treatment_provided="Manual therapy + supervised exercises",
                    notes=f"Session {number}: progressing as expected",
                )
            )
        packages.append(package)
        db.flush()

    logger.info("Treatment packages: %d", len(packages))
    return packages


def seed_appointments(
    db: Session, patients: list[Patient], packages: list[TreatmentPackage], creator: User
) -> list[Appointment]:
    today = date.today()
    package_by_patient = {package.patient_id: package for package in packages}

    specs = [
        (patients[0], today, time(9, 30), AppointmentStatus.CONFIRMED),
        (patients[1], today, time(10, 0), AppointmentStatus.CHECKED_IN),
        (patients[2], today, time(17, 30), AppointmentStatus.BOOKED),
        (patients[3], today, time(18, 0), AppointmentStatus.BOOKED),
        (patients[0], today - timedelta(days=2), time(9, 30), AppointmentStatus.COMPLETED),
        (patients[1], today - timedelta(days=3), time(10, 0), AppointmentStatus.COMPLETED),
        (patients[4], today - timedelta(days=1), time(16, 30), AppointmentStatus.CANCELLED),
        (patients[2], today + timedelta(days=1), time(17, 0), AppointmentStatus.BOOKED),
        (patients[4], today + timedelta(days=2), time(18, 30), AppointmentStatus.BOOKED),
    ]

    appointments: list[Appointment] = []
    for patient, on_date, start, status in specs:
        existing = db.execute(
            select(Appointment).where(
                Appointment.patient_id == patient.id,
                Appointment.appointment_date == on_date,
                Appointment.start_time == start,
            )
        ).scalar_one_or_none()
        if existing:
            appointments.append(existing)
            continue

        clinic_id = patient.primary_clinic_id
        clinic = db.get(Clinic, clinic_id)
        duration = settings.DEFAULT_SLOT_DURATION_MINUTES
        end = (datetime.combine(on_date, start) + timedelta(minutes=duration)).time()

        appointment = Appointment(
            appointment_code=generate_appointment_code(db, on_date, clinic.code),
            patient_id=patient.id,
            clinic_id=clinic_id,
            appointment_date=on_date,
            start_time=start,
            end_time=end,
            duration_minutes=duration,
            status=status,
            chief_complaint=patient.chief_complaint,
            package_id=(
                package_by_patient[patient.id].id if patient.id in package_by_patient else None
            ),
            created_by_user_id=creator.id,
        )
        if status == AppointmentStatus.COMPLETED:
            appointment.completed_at = datetime.combine(on_date, end)
        elif status == AppointmentStatus.CHECKED_IN:
            appointment.checked_in_at = datetime.now(timezone.utc)
        elif status == AppointmentStatus.CANCELLED:
            appointment.cancelled_at = datetime.now(timezone.utc)
            appointment.cancellation_reason = "Patient unwell"
        elif status == AppointmentStatus.CONFIRMED:
            appointment.confirmed_at = datetime.now(timezone.utc)

        db.add(appointment)
        db.flush()

        db.add(
            AppointmentHistory(
                appointment_id=appointment.id,
                action=AppointmentAction.CREATED,
                new_date=on_date,
                new_time=start,
                new_status=AppointmentStatus.BOOKED,
                new_clinic_id=clinic_id,
                changed_by_user_id=creator.id,
                reason="Seed data",
            )
        )
        if status != AppointmentStatus.BOOKED:
            db.add(
                AppointmentHistory(
                    appointment_id=appointment.id,
                    action=AppointmentAction[status.value],
                    old_status=AppointmentStatus.BOOKED,
                    new_status=status,
                    changed_by_user_id=creator.id,
                )
            )
        appointments.append(appointment)

    db.flush()
    logger.info("Appointments: %d", len(appointments))
    return appointments


def seed_bills(db: Session, packages: list[TreatmentPackage], creator: User) -> list[Bill]:
    from app.utils.identifiers import generate_bill_number

    payment_plan = [
        (Decimal("1.00"), PaymentMethod.UPI),      # fully paid
        (Decimal("0.50"), PaymentMethod.CASH),     # half paid
        (Decimal("0.00"), PaymentMethod.CASH),     # unpaid
        (Decimal("1.00"), PaymentMethod.CARD),     # fully paid
    ]

    bills: list[Bill] = []
    for package, (paid_ratio, method) in zip(packages, payment_plan):
        existing = db.execute(
            select(Bill).where(Bill.package_id == package.id)
        ).scalar_one_or_none()
        if existing:
            bills.append(existing)
            continue

        total = (package.price_per_session * package.sessions_registered).quantize(
            Decimal("0.01")
        )
        paid = (total * paid_ratio).quantize(Decimal("0.01"))
        if paid <= 0:
            payment_status = PaymentStatus.UNPAID
        elif paid >= total:
            payment_status = PaymentStatus.PAID
        else:
            payment_status = PaymentStatus.PARTIAL

        bill = Bill(
            bill_number=generate_bill_number(db),
            clinic_id=package.clinic_id,
            patient_id=package.patient_id,
            package_id=package.id,
            bill_date=package.start_date or date.today(),
            sessions_purchased=package.sessions_registered,
            sessions_taken_snapshot=package.sessions_taken,
            price_per_session=package.price_per_session,
            subtotal_amount=total,
            total_amount=total,
            amount_paid=paid,
            status=BillStatus.FINALIZED,
            payment_status=payment_status,
            created_by_user_id=creator.id,
        )
        db.add(bill)
        db.flush()

        db.add(
            BillItem(
                bill_id=bill.id,
                item_type=BillItemType.SESSION_PACKAGE,
                description=f"Physiotherapy package - {package.sessions_registered} sessions",
                quantity=package.sessions_registered,
                unit_price=package.price_per_session,
                amount=total,
            )
        )
        if paid > 0:
            db.add(
                Payment(
                    bill_id=bill.id,
                    amount=paid,
                    payment_method=method,
                    payment_date=bill.bill_date,
                    reference_number=f"SEED-{bill.bill_number}",
                    received_by_user_id=creator.id,
                )
            )
        bills.append(bill)

    db.flush()
    logger.info("Bills: %d", len(bills))
    return bills


def seed_notifications(db: Session, appointments: list[Appointment]) -> None:
    upcoming = [
        appointment
        for appointment in appointments
        if appointment.appointment_date >= date.today()
        and appointment.status == AppointmentStatus.BOOKED
    ][:3]

    for appointment in upcoming:
        existing = db.execute(
            select(Notification).where(Notification.appointment_id == appointment.id)
        ).scalar_one_or_none()
        if existing:
            continue
        patient = appointment.patient
        db.add(
            Notification(
                clinic_id=appointment.clinic_id,
                notification_type=NotificationType.NEW_APPOINTMENT,
                title="NEW APPOINTMENT",
                message=(
                    f"{patient.full_name} booked for "
                    f"{appointment.appointment_date:%d-%b-%Y} at "
                    f"{appointment.start_time:%I:%M %p}"
                ),
                # Keys and formats must match what notification_service writes at
                # runtime, or the seeded cards render differently from real ones
                # and the difference looks like an app bug.
                payload={
                    "event": "created",
                    "appointment_code": appointment.appointment_code,
                    "patient_name": patient.full_name,
                    "patient_code": patient.patient_code,
                    "clinic_name": appointment.clinic.name,
                    "date": appointment.appointment_date.isoformat(),
                    "time": notification_service.slot_time_text(appointment),
                    "booked_by": "Seed data",
                },
                appointment_id=appointment.id,
                patient_id=patient.id,
            )
        )
    db.flush()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_seed(reset: bool = False) -> None:
    if reset:
        logger.warning("Dropping all tables in %s", settings.safe_database_url)
        Base.metadata.drop_all(bind=engine)

    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        roles = seed_roles(db)
        sources = seed_patient_sources(db)
        seed_service_items(db)

        superadmin = _get_or_create_user(
            db,
            username=settings.SEED_SUPERADMIN_USERNAME,
            password=settings.SEED_SUPERADMIN_PASSWORD,
            full_name="System Superadmin",
            role=roles[RoleName.SUPERADMIN],
            email=settings.SEED_SUPERADMIN_EMAIL,
        )
        admin = _get_or_create_user(
            db,
            username=settings.SEED_ADMIN_USERNAME,
            password=settings.SEED_ADMIN_PASSWORD,
            full_name="Operations Admin",
            role=roles[RoleName.ADMIN],
            email="admin@clinic.example.com",
            phone="9876500001",
        )

        clinics = seed_clinics(db)
        clinic_users = seed_clinic_users(db, clinics, roles[RoleName.CLINIC_USER])
        therapist = clinic_users[1] if len(clinic_users) > 1 else clinic_users[0]

        patients = seed_patients(db, clinics, sources, admin)
        packages = seed_packages_and_sessions(db, patients, therapist)
        appointments = seed_appointments(db, patients, packages, admin)
        seed_bills(db, packages, therapist)
        seed_notifications(db, appointments)

        db.commit()

        totals = {
            "users": db.execute(select(func.count()).select_from(User)).scalar_one(),
            "clinics": db.execute(select(func.count()).select_from(Clinic)).scalar_one(),
            "patients": db.execute(select(func.count()).select_from(Patient)).scalar_one(),
            "appointments": db.execute(select(func.count()).select_from(Appointment)).scalar_one(),
            "sessions": db.execute(select(func.count()).select_from(PatientSession)).scalar_one(),
            "bills": db.execute(select(func.count()).select_from(Bill)).scalar_one(),
        }

    logger.info("Seed complete: %s", totals)
    print("\n" + "=" * 62)
    print("  DEVELOPMENT LOGIN CREDENTIALS")
    print("=" * 62)
    print(f"  Superadmin  : {settings.SEED_SUPERADMIN_USERNAME} / {settings.SEED_SUPERADMIN_PASSWORD}")
    print(f"  Admin       : {settings.SEED_ADMIN_USERNAME} / {settings.SEED_ADMIN_PASSWORD}")
    print(f"  Clinic User : hsr.reception / {settings.SEED_CLINIC_USER_PASSWORD}   (HSR Layout)")
    print(f"  Clinic User : hsr.physio    / {settings.SEED_CLINIC_USER_PASSWORD}   (HSR Layout)")
    print(f"  Clinic User : btm.reception / {settings.SEED_CLINIC_USER_PASSWORD}   (BTM Layout)")
    print("=" * 62)
    print("  Change these before exposing the app to anyone else.")
    print("=" * 62 + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the clinic management database")
    parser.add_argument(
        "--reset", action="store_true", help="Drop all tables before seeding (destructive)"
    )
    args = parser.parse_args()

    if args.reset and settings.is_production:
        logger.error("Refusing to drop tables while APP_ENV=production")
        return 1

    run_seed(reset=args.reset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
