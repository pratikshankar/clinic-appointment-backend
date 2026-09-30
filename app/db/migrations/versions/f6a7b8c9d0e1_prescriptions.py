"""Add prescriptions table.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prescriptions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("patient_id", sa.Integer, sa.ForeignKey("patients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("clinic_id", sa.Integer, sa.ForeignKey("clinics.id", ondelete="SET NULL"), nullable=True),
        sa.Column("prescribed_by_user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("chief_complaint", sa.Text, nullable=True),
        sa.Column("history", sa.Text, nullable=True),
        sa.Column("on_examination", sa.Text, nullable=True),
        sa.Column("diagnosis", sa.Text, nullable=True),
        sa.Column("treatment_plan", sa.Text, nullable=True),
        sa.Column("home_protocol", sa.Text, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_prescriptions_patient_id", "prescriptions", ["patient_id"])


def downgrade() -> None:
    op.drop_index("ix_prescriptions_patient_id", table_name="prescriptions")
    op.drop_table("prescriptions")
