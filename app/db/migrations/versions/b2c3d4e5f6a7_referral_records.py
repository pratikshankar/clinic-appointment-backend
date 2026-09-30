"""Referral records table.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-25
"""

from alembic import op
import sqlalchemy as sa

revision: str = 'b2c3d4e5f6a7'
down_revision: str | None = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'referral_records',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('referrer_patient_id', sa.Integer(),
                  sa.ForeignKey('patients.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('referred_patient_id', sa.Integer(),
                  sa.ForeignKey('patients.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('referred_package_id', sa.Integer(),
                  sa.ForeignKey('treatment_packages.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('clinic_id', sa.Integer(),
                  sa.ForeignKey('clinics.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='PENDING'),
        sa.Column('credit_sessions', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('credited_to_package_id', sa.Integer(),
                  sa.ForeignKey('treatment_packages.id', ondelete='SET NULL'), nullable=True),
        sa.Column('credited_by_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('credited_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('voided_reason', sa.String(500), nullable=True),
        sa.Column('initiated_by_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint('credit_sessions > 0', name='ck_referral_credit_positive'),
        sa.CheckConstraint(
            'referrer_patient_id != referred_patient_id',
            name='ck_referral_no_self_referral',
        ),
    )
    op.create_index('ix_referrals_referrer', 'referral_records', ['referrer_patient_id'])
    op.create_index('ix_referrals_referred', 'referral_records', ['referred_patient_id'])
    op.create_index('ix_referrals_package', 'referral_records', ['referred_package_id'])
    op.create_index('ix_referrals_status', 'referral_records', ['status'])
    op.create_index('ix_referrals_clinic', 'referral_records', ['clinic_id'])


def downgrade() -> None:
    op.drop_table('referral_records')
