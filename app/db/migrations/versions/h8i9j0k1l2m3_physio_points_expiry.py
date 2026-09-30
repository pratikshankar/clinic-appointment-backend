"""Physio Points expiry and redemption-value fields.

Revision ID: h8i9j0k1l2m3
Revises: g7h8i9j0k1l2
Create Date: 2026-09-30

Changes
-------
* physio_points_ledger.expires_at  — DATE, nullable (set only on EARNED entries)
* physio_points_ledger.source_entry_id — INTEGER, nullable (EXPIRED → EARNED ref)
* EXPIRED added to points_transaction_type enum (Postgres)
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "h8i9j0k1l2m3"
down_revision: str | None = "g7h8i9j0k1l2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "physio_points_ledger",
        sa.Column("expires_at", sa.Date, nullable=True),
    )
    op.add_column(
        "physio_points_ledger",
        sa.Column("source_entry_id", sa.Integer, nullable=True),
    )
    # Extend the enum type in Postgres (no-op for SQLite which uses VARCHAR)
    op.execute(
        "ALTER TYPE points_transaction_type ADD VALUE IF NOT EXISTS 'EXPIRED'"
    )


def downgrade() -> None:
    op.drop_column("physio_points_ledger", "source_entry_id")
    op.drop_column("physio_points_ledger", "expires_at")
    # Note: removing an enum value in Postgres requires recreating the type.
    # Downgrade leaves EXPIRED in the enum rather than corrupt existing rows.
