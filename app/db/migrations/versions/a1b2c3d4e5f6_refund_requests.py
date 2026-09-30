"""Refund requests and attachments tables.

Revision ID: a1b2c3d4e5f6
Revises: e20e54ab7386
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa

revision: str = 'a1b2c3d4e5f6'
down_revision: str | None = 'e20e54ab7386'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'refund_requests',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('package_id', sa.Integer(), sa.ForeignKey('treatment_packages.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('bill_id', sa.Integer(), sa.ForeignKey('bills.id', ondelete='SET NULL'), nullable=True),
        sa.Column('patient_id', sa.Integer(), sa.ForeignKey('patients.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('clinic_id', sa.Integer(), sa.ForeignKey('clinics.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('status', sa.String(30), nullable=False, server_default='PENDING_APPROVAL'),
        sa.Column('initiated_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('sessions_consumed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('sessions_registered', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('single_session_rate', sa.Numeric(12, 2), nullable=False, server_default='0'),
        sa.Column('package_session_rate', sa.Numeric(12, 2), nullable=False, server_default='0'),
        sa.Column('consultation_fee', sa.Numeric(12, 2), nullable=False, server_default='0'),
        sa.Column('total_paid', sa.Numeric(12, 2), nullable=False, server_default='0'),
        sa.Column('deduction_amount', sa.Numeric(12, 2), nullable=False, server_default='0'),
        sa.Column('refund_amount', sa.Numeric(12, 2), nullable=False, server_default='0'),
        sa.Column('calculation_notes', sa.Text(), nullable=True),
        sa.Column('payment_method', sa.String(20), nullable=True),
        sa.Column('bank_account_name', sa.String(120), nullable=True),
        sa.Column('bank_account_number', sa.String(30), nullable=True),
        sa.Column('bank_ifsc', sa.String(15), nullable=True),
        sa.Column('upi_id', sa.String(60), nullable=True),
        sa.Column('reviewed_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('review_notes', sa.Text(), nullable=True),
        sa.Column('completed_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('payment_reference', sa.String(120), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint('refund_amount >= 0', name='ck_refund_amount_non_negative'),
        sa.CheckConstraint('deduction_amount >= 0', name='ck_refund_deduction_non_negative'),
    )
    op.create_index('ix_refund_requests_clinic', 'refund_requests', ['clinic_id'])
    op.create_index('ix_refund_requests_patient', 'refund_requests', ['patient_id'])
    op.create_index('ix_refund_requests_status', 'refund_requests', ['status'])
    op.create_index('ix_refund_requests_package', 'refund_requests', ['package_id'])

    op.create_table(
        'refund_attachments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('refund_request_id', sa.Integer(), sa.ForeignKey('refund_requests.id', ondelete='CASCADE'), nullable=False),
        sa.Column('uploaded_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('attachment_type', sa.String(30), nullable=False),
        sa.Column('file_name', sa.String(255), nullable=False),
        sa.Column('mime_type', sa.String(100), nullable=False),
        sa.Column('file_content', sa.LargeBinary(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_refund_attachments_request', 'refund_attachments', ['refund_request_id'])


def downgrade() -> None:
    op.drop_table('refund_attachments')
    op.drop_table('refund_requests')
