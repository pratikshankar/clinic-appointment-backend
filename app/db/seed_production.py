"""Production bootstrap: create superadmin and admin users only.

Run ONCE after applying supabase_schema.sql to a fresh database.
Reads credentials from .env (CLINIC_SEED_* variables) — never hardcoded.

Usage:
    python -m app.db.seed_production
"""

import logging
import sys

from sqlalchemy import select

from app.auth.security import hash_password
from app.config import settings
from app.db.database import SessionLocal
from app.models import Role, RoleName, User

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
logger = logging.getLogger("seed_production")


def _get_or_create_user(db, *, username, password, full_name, email, role):
    existing = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if existing:
        logger.info("User %s already exists — skipped", username)
        return existing
    user = User(
        username=username,
        email=email,
        full_name=full_name,
        hashed_password=hash_password(password),
        role_id=role.id,
        is_active=True,
    )
    db.add(user)
    db.flush()
    logger.info("Created %s (%s)", username, role.name.value)
    return user


def main() -> int:
    with SessionLocal() as db:
        roles = {
            row.name: row
            for row in db.execute(select(Role)).scalars().all()
        }
        if not roles:
            logger.error("Roles table is empty — run supabase_schema.sql first")
            return 1

        _get_or_create_user(
            db,
            username=settings.SEED_SUPERADMIN_USERNAME,
            password=settings.SEED_SUPERADMIN_PASSWORD,
            full_name="System Superadmin",
            email=settings.SEED_SUPERADMIN_EMAIL,
            role=roles[RoleName.SUPERADMIN],
        )
        _get_or_create_user(
            db,
            username=settings.SEED_ADMIN_USERNAME,
            password=settings.SEED_ADMIN_PASSWORD,
            full_name="Operations Admin",
            email="admin@paineasy.in",
            role=roles[RoleName.ADMIN],
        )

        db.commit()

    print()
    print("=" * 60)
    print("  PRODUCTION LOGIN CREDENTIALS")
    print("=" * 60)
    print(f"  Superadmin : {settings.SEED_SUPERADMIN_USERNAME} / {settings.SEED_SUPERADMIN_PASSWORD}")
    print(f"  Admin      : {settings.SEED_ADMIN_USERNAME} / {settings.SEED_ADMIN_PASSWORD}")
    print("=" * 60)
    print("  Change these passwords immediately after first login.")
    print("=" * 60)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
