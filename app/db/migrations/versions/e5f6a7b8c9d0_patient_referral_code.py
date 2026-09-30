"""Add referral_code and referred_by_code to patients.

referral_code  — unique shareable code, auto-set to REF-<patient_code>.
referred_by_code — the referral code a new patient gave at registration.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-28
"""

from alembic import op
import sqlalchemy as sa

revision: str = 'e5f6a7b8c9d0'
down_revision: str | None = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('patients', sa.Column('referral_code', sa.String(30), nullable=True))
    op.add_column('patients', sa.Column('referred_by_code', sa.String(30), nullable=True))
    op.create_unique_constraint('uq_patients_referral_code', 'patients', ['referral_code'])
    op.create_index('ix_patients_referral_code', 'patients', ['referral_code'])

    # Backfill existing patients with REF-<patient_code>
    op.execute(
        "UPDATE patients SET referral_code = 'REF-' || patient_code WHERE referral_code IS NULL"
    )


def downgrade() -> None:
    op.drop_index('ix_patients_referral_code', table_name='patients')
    op.drop_constraint('uq_patients_referral_code', 'patients', type_='unique')
    op.drop_column('patients', 'referred_by_code')
    op.drop_column('patients', 'referral_code')
