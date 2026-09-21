"""Shared pytest fixtures.

The environment is configured *before* any application module is imported, so
`app.config.settings` resolves to an isolated temporary SQLite file and the test
run can never touch the development database.
"""

import os
import tempfile
from datetime import date, time
from decimal import Decimal

import pytest

_TMP_DIR = tempfile.mkdtemp(prefix="clinic-tests-")
os.environ["CLINIC_DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP_DIR, 'test.db')}"
os.environ["CLINIC_APP_ENV"] = "test"
os.environ["CLINIC_AUTO_CREATE_TABLES"] = "false"
os.environ["CLINIC_SECRET_KEY"] = "test-only-secret-key-at-least-32-chars-long"
os.environ["CLINIC_ACCESS_TOKEN_EXPIRE_MINUTES"] = "30"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.auth.security import hash_password  # noqa: E402
from app.db.database import Base, SessionLocal, engine, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Bill,
    BillStatus,
    Clinic,
    ClinicStatus,
    ClinicUser,
    Patient,
    PaymentStatus,
    Role,
    RoleName,
    User,
)

TEST_PASSWORD = "TestPass@123"


@pytest.fixture(autouse=True)
def _fresh_schema():
    """Every test starts from an empty schema."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db() -> Session:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db) -> TestClient:
    """TestClient sharing the fixture session, so writes are visible to both."""

    def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Data fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def roles(db) -> dict[RoleName, Role]:
    created = {}
    for name in RoleName:
        role = Role(name=name, description=name.value)
        db.add(role)
        created[name] = role
    db.commit()
    return created


@pytest.fixture
def clinics(db) -> list[Clinic]:
    from app.services.clinic_service import default_working_hours

    made = []
    for name, code, city in [("HSR Layout", "HSR", "Bengaluru"), ("BTM Layout", "BTM", "Bengaluru")]:
        clinic = Clinic(
            name=name,
            code=code,
            location=name,
            city=city,
            state="Karnataka",
            pin_code="560102",
            phone="9876500011",
            status=ClinicStatus.ACTIVE,
            slot_duration_minutes=30,
            capacity_per_slot=3,
        )
        clinic.working_hours = default_working_hours()
        db.add(clinic)
        made.append(clinic)

    inactive = Clinic(
        name="Whitefield (closed)",
        code="WHF",
        location="Whitefield",
        city="Bengaluru",
        status=ClinicStatus.INACTIVE,
        slot_duration_minutes=30,
        capacity_per_slot=2,
    )
    db.add(inactive)
    made.append(inactive)
    db.commit()
    return made


def _make_user(db, roles, username, role_name, clinic=None, is_active=True) -> User:
    user = User(
        username=username,
        email=f"{username}@clinic.example.com",
        full_name=username.replace(".", " ").title(),
        hashed_password=hash_password(TEST_PASSWORD),
        role_id=roles[role_name].id,
        is_active=is_active,
    )
    db.add(user)
    db.flush()
    if clinic is not None:
        db.add(ClinicUser(user_id=user.id, clinic_id=clinic.id, is_primary=True))
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def superadmin(db, roles) -> User:
    return _make_user(db, roles, "superadmin", RoleName.SUPERADMIN)


@pytest.fixture
def admin(db, roles) -> User:
    return _make_user(db, roles, "admin", RoleName.ADMIN)


@pytest.fixture
def hsr_user(db, roles, clinics) -> User:
    return _make_user(db, roles, "hsr.reception", RoleName.CLINIC_USER, clinic=clinics[0])


@pytest.fixture
def btm_user(db, roles, clinics) -> User:
    return _make_user(db, roles, "btm.reception", RoleName.CLINIC_USER, clinic=clinics[1])


@pytest.fixture
def unassigned_user(db, roles) -> User:
    """A Clinic User with no clinic assignment -- must see nothing, not everything."""
    return _make_user(db, roles, "orphan.user", RoleName.CLINIC_USER)


@pytest.fixture
def auth(client):
    """Return an Authorization header factory: `auth("username")`."""

    def _login(username: str, password: str = TEST_PASSWORD) -> dict[str, str]:
        response = client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    return _login


@pytest.fixture
def sample_clinical_data(db, clinics, admin):
    """Patients, appointments and bills with known totals for dashboard tests."""
    today = date.today()
    hsr, btm = clinics[0], clinics[1]

    patients = []
    for index, (name, mobile, clinic) in enumerate(
        [
            ("Rahul Sharma", "9876543210", hsr),
            ("Anita Desai", "9876543211", hsr),
            ("Meera Krishnan", "9876543213", btm),
        ]
    ):
        patient = Patient(
            patient_code=f"PT-{index + 1:06d}",
            full_name=name,
            mobile=mobile,
            primary_clinic_id=clinic.id,
            registration_date=today,
            is_profile_complete=True,
        )
        db.add(patient)
        patients.append(patient)
    db.flush()

    # 2 appointments today at HSR (1 completed), 1 today at BTM.
    specs = [
        (patients[0], hsr, today, time(9, 30), AppointmentStatus.COMPLETED),
        (patients[1], hsr, today, time(10, 0), AppointmentStatus.BOOKED),
        (patients[2], btm, today, time(17, 30), AppointmentStatus.BOOKED),
    ]
    for index, (patient, clinic, on_date, start, status) in enumerate(specs):
        db.add(
            Appointment(
                appointment_code=f"APT-TEST-{index:04d}",
                patient_id=patient.id,
                clinic_id=clinic.id,
                appointment_date=on_date,
                start_time=start,
                end_time=time(start.hour, 30) if start.minute == 0 else time(start.hour + 1, 0),
                duration_minutes=30,
                status=status,
            )
        )

    # HSR: billed 5000, collected 5000. BTM: billed 4000, collected 1000.
    db.add(
        Bill(
            bill_number="INV-TEST-0001",
            clinic_id=hsr.id,
            patient_id=patients[0].id,
            bill_date=today,
            subtotal_amount=Decimal("5000.00"),
            total_amount=Decimal("5000.00"),
            amount_paid=Decimal("5000.00"),
            status=BillStatus.FINALIZED,
            payment_status=PaymentStatus.PAID,
        )
    )
    db.add(
        Bill(
            bill_number="INV-TEST-0002",
            clinic_id=btm.id,
            patient_id=patients[2].id,
            bill_date=today,
            subtotal_amount=Decimal("4000.00"),
            total_amount=Decimal("4000.00"),
            amount_paid=Decimal("1000.00"),
            status=BillStatus.FINALIZED,
            payment_status=PaymentStatus.PARTIAL,
        )
    )
    # A cancelled bill must be excluded from every revenue figure.
    db.add(
        Bill(
            bill_number="INV-TEST-0003",
            clinic_id=hsr.id,
            patient_id=patients[1].id,
            bill_date=today,
            subtotal_amount=Decimal("9999.00"),
            total_amount=Decimal("9999.00"),
            amount_paid=Decimal("0.00"),
            status=BillStatus.CANCELLED,
            payment_status=PaymentStatus.UNPAID,
        )
    )
    db.commit()
    return {"patients": patients, "clinics": clinics}
