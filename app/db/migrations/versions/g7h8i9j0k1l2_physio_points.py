"""Add Physio Points loyalty programme.

Revision ID: g7h8i9j0k1l2
Revises: f6a7b8c9d0e1
Create Date: 2026-09-29
"""
from __future__ import annotations
import sqlalchemy as sa
from alembic import op

revision: str = "g7h8i9j0k1l2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("patients", sa.Column("physio_points", sa.Integer, nullable=False, server_default="0"))
    op.execute("UPDATE patients SET physio_points = 0")

    op.create_table(
        "physio_points_ledger",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("patient_id", sa.Integer, sa.ForeignKey("patients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("points", sa.Integer, nullable=False),
        sa.Column("transaction_type", sa.String(30), nullable=False),
        sa.Column("payment_id", sa.Integer, sa.ForeignKey("payments.id", ondelete="SET NULL"), nullable=True),
        sa.Column("bill_id", sa.Integer, sa.ForeignKey("bills.id", ondelete="SET NULL"), nullable=True),
        sa.Column("refund_id", sa.Integer, sa.ForeignKey("refund_requests.id", ondelete="SET NULL"), nullable=True),
        sa.Column("description", sa.String(500), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("created_by_user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_physio_points_ledger_patient_id", "physio_points_ledger", ["patient_id"])


def downgrade() -> None:
    op.drop_index("ix_physio_points_ledger_patient_id", table_name="physio_points_ledger")
    op.drop_table("physio_points_ledger")
    op.drop_column("patients", "physio_points")
