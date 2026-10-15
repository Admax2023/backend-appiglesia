"""initial schema

Full schema for the church fried-food business: users/auth, settings (singleton), products,
customers, orders (+items, status history), payment proofs, payments, debt write-offs,
cash sessions/movements/count lines, audit log and idempotency keys.

Hand-checked additions over autogenerate:
  * default business_settings row (id = 1)
  * triggers that make CLOSED cash sessions immutable (session row, its movements and count lines)
  * downgrade drops the PostgreSQL enum types

Revision ID: 0001
Revises: 
Create Date: 2026-10-11
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('customers',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('phone', sa.String(length=30), nullable=True),
    sa.Column('phone_normalized', sa.String(length=20), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_customers'))
    )
    op.create_index('ix_customers_name', 'customers', ['name'], unique=False)
    op.create_index('uq_customers_phone_normalized', 'customers', ['phone_normalized'], unique=True, postgresql_where=sa.text('phone_normalized IS NOT NULL'))
    op.create_table('idempotency_keys',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('scope', sa.String(length=120), nullable=False),
    sa.Column('key', sa.String(length=255), nullable=False),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.Column('response_status', sa.SmallInteger(), nullable=True),
    sa.Column('response_body', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_idempotency_keys')),
    sa.UniqueConstraint('scope', 'key', name='uq_idempotency_keys_scope_key')
    )
    op.create_index('ix_idempotency_keys_created_at', 'idempotency_keys', ['created_at'], unique=False)
    op.create_table('products',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('price', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('is_available', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('sort_order', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('length(trim(name)) > 0', name=op.f('ck_products_name_not_blank')),
    sa.CheckConstraint('price >= 0', name=op.f('ck_products_price_non_negative')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_products'))
    )
    op.create_index('ix_products_active_sort', 'products', ['is_active', 'sort_order'], unique=False)
    op.create_table('users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('full_name', sa.String(length=120), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.Enum('admin', name='user_role'), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('length(username) >= 3', name=op.f('ck_users_username_min_length')),
    sa.CheckConstraint('username = lower(username)', name=op.f('ck_users_username_lowercase')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('username', name=op.f('uq_users_username'))
    )
    op.create_table('audit_logs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=True),
    sa.Column('action', sa.String(length=60), nullable=False),
    sa.Column('entity_type', sa.String(length=40), nullable=False),
    sa.Column('entity_id', sa.UUID(), nullable=True),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_audit_logs_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_logs'))
    )
    op.create_index('ix_audit_logs_action', 'audit_logs', ['action'], unique=False)
    op.create_index('ix_audit_logs_created_at', 'audit_logs', ['created_at'], unique=False)
    op.create_index('ix_audit_logs_entity', 'audit_logs', ['entity_type', 'entity_id'], unique=False)
    op.create_table('business_settings',
    sa.Column('id', sa.SmallInteger(), nullable=False),
    sa.Column('business_name', sa.String(length=120), nullable=False),
    sa.Column('accepting_orders', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('cash_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('transfer_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('credit_enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('allow_partial_payments', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('bank_name', sa.String(length=120), nullable=True),
    sa.Column('bank_account_type', sa.String(length=60), nullable=True),
    sa.Column('bank_account_number', sa.String(length=60), nullable=True),
    sa.Column('bank_account_holder', sa.String(length=120), nullable=True),
    sa.Column('bank_holder_id', sa.String(length=60), nullable=True),
    sa.Column('transfer_instructions', sa.Text(), nullable=True),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_by_id', sa.UUID(), nullable=True),
    sa.CheckConstraint('cash_enabled OR transfer_enabled OR credit_enabled', name=op.f('ck_business_settings_one_method_enabled')),
    sa.CheckConstraint('id = 1', name=op.f('ck_business_settings_singleton')),
    sa.ForeignKeyConstraint(['updated_by_id'], ['users.id'], name=op.f('fk_business_settings_updated_by_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_business_settings'))
    )
    op.create_table('cash_sessions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('status', sa.Enum('open', 'closed', name='cash_session_status'), nullable=False),
    sa.Column('opening_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('opening_note', sa.String(length=500), nullable=True),
    sa.Column('opened_by_id', sa.UUID(), nullable=True),
    sa.Column('opened_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('closed_by_id', sa.UUID(), nullable=True),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expected_cash', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('counted_cash', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('difference', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('closing_note', sa.String(length=1000), nullable=True),
    sa.CheckConstraint("status <> 'closed' OR (closed_at IS NOT NULL AND expected_cash IS NOT NULL AND counted_cash IS NOT NULL AND difference IS NOT NULL)", name=op.f('ck_cash_sessions_closed_fields')),
    sa.CheckConstraint('counted_cash IS NULL OR counted_cash >= 0', name=op.f('ck_cash_sessions_counted_non_negative')),
    sa.CheckConstraint('difference IS NULL OR difference = 0 OR (closing_note IS NOT NULL AND length(trim(closing_note)) > 0)', name=op.f('ck_cash_sessions_note_when_difference')),
    sa.CheckConstraint('opening_amount >= 0', name=op.f('ck_cash_sessions_opening_non_negative')),
    sa.ForeignKeyConstraint(['closed_by_id'], ['users.id'], name=op.f('fk_cash_sessions_closed_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['opened_by_id'], ['users.id'], name=op.f('fk_cash_sessions_opened_by_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cash_sessions'))
    )
    op.create_index('ix_cash_sessions_opened_at', 'cash_sessions', ['opened_at'], unique=False)
    op.create_index('uq_cash_sessions_single_open', 'cash_sessions', ['status'], unique=True, postgresql_where=sa.text("status = 'open'"))
    op.create_table('orders',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('code', sa.String(length=12), nullable=False),
    sa.Column('tracking_token_hash', sa.String(length=64), nullable=False),
    sa.Column('customer_id', sa.UUID(), nullable=False),
    sa.Column('customer_name', sa.String(length=120), nullable=False),
    sa.Column('customer_phone', sa.String(length=30), nullable=True),
    sa.Column('customer_phone_normalized', sa.String(length=20), nullable=True),
    sa.Column('notes', sa.String(length=500), nullable=True),
    sa.Column('source', sa.String(length=10), server_default='web', nullable=False),
    sa.Column('payment_method', sa.Enum('cash', 'transfer', 'credit', name='payment_method'), nullable=False),
    sa.Column('status', sa.Enum('pending_confirmation', 'confirmed', 'preparing', 'ready', 'delivered', 'cancelled', name='order_status'), nullable=False),
    sa.Column('payment_status', sa.Enum('unpaid', 'partial', 'paid', 'refunded', 'written_off', name='payment_status'), nullable=False),
    sa.Column('credit_status', sa.Enum('none', 'pending_authorization', 'authorized', 'rejected', name='credit_status'), nullable=False),
    sa.Column('total', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('amount_paid', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
    sa.Column('amount_written_off', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
    sa.Column('balance_due', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('credit_reviewed_by_id', sa.UUID(), nullable=True),
    sa.Column('credit_reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('credit_note', sa.String(length=500), nullable=True),
    sa.Column('cancel_reason', sa.String(length=500), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(payment_method = 'credit') = (credit_status <> 'none') OR credit_status IN ('authorized','rejected')", name=op.f('ck_orders_credit_status_consistent')),
    sa.CheckConstraint('amount_paid + amount_written_off <= total', name=op.f('ck_orders_no_overpayment')),
    sa.CheckConstraint('amount_paid >= 0', name=op.f('ck_orders_amount_paid_non_negative')),
    sa.CheckConstraint('amount_written_off >= 0', name=op.f('ck_orders_written_off_non_negative')),
    sa.CheckConstraint('balance_due >= 0', name=op.f('ck_orders_balance_non_negative')),
    sa.CheckConstraint('total >= 0', name=op.f('ck_orders_total_non_negative')),
    sa.ForeignKeyConstraint(['credit_reviewed_by_id'], ['users.id'], name=op.f('fk_orders_credit_reviewed_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_orders_customer_id_customers'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_orders')),
    sa.UniqueConstraint('code', name=op.f('uq_orders_code')),
    sa.UniqueConstraint('tracking_token_hash', name=op.f('uq_orders_tracking_token_hash'))
    )
    op.create_index('ix_orders_created_at', 'orders', ['created_at'], unique=False)
    op.create_index('ix_orders_customer_id', 'orders', ['customer_id'], unique=False)
    op.create_index('ix_orders_customer_phone_created', 'orders', ['customer_phone_normalized', 'created_at'], unique=False)
    op.create_index('ix_orders_payment_status', 'orders', ['payment_status'], unique=False)
    op.create_index('ix_orders_status', 'orders', ['status'], unique=False)
    op.create_table('refresh_tokens',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('family_id', sa.UUID(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('replaced_by_id', sa.UUID(), nullable=True),
    sa.Column('user_agent', sa.String(length=255), nullable=True),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['replaced_by_id'], ['refresh_tokens.id'], name=op.f('fk_refresh_tokens_replaced_by_id_refresh_tokens'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_refresh_tokens_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_refresh_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_refresh_tokens_token_hash'))
    )
    op.create_index('ix_refresh_tokens_family_id', 'refresh_tokens', ['family_id'], unique=False)
    op.create_index('ix_refresh_tokens_user_id', 'refresh_tokens', ['user_id'], unique=False)
    op.create_table('cash_count_lines',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('session_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.Enum('bill', 'coin', name='denomination_kind'), nullable=False),
    sa.Column('denomination', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('subtotal', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.CheckConstraint('denomination > 0', name=op.f('ck_cash_count_lines_denomination_positive')),
    sa.CheckConstraint('quantity >= 0', name=op.f('ck_cash_count_lines_quantity_non_negative')),
    sa.CheckConstraint('subtotal = denomination * quantity', name=op.f('ck_cash_count_lines_subtotal_consistent')),
    sa.ForeignKeyConstraint(['session_id'], ['cash_sessions.id'], name=op.f('fk_cash_count_lines_session_id_cash_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cash_count_lines')),
    sa.UniqueConstraint('session_id', 'kind', 'denomination', name='uq_cash_count_lines_session_kind_denom')
    )
    op.create_table('debt_writeoffs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('reason', sa.String(length=500), nullable=False),
    sa.Column('created_by_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('amount > 0', name=op.f('ck_debt_writeoffs_amount_positive')),
    sa.CheckConstraint('length(trim(reason)) > 0', name=op.f('ck_debt_writeoffs_reason_not_blank')),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_debt_writeoffs_created_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_debt_writeoffs_order_id_orders'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_debt_writeoffs'))
    )
    op.create_index('ix_debt_writeoffs_order_id', 'debt_writeoffs', ['order_id'], unique=False)
    op.create_table('order_items',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=False),
    sa.Column('product_id', sa.UUID(), nullable=False),
    sa.Column('position', sa.SmallInteger(), nullable=False),
    sa.Column('product_name', sa.String(length=120), nullable=False),
    sa.Column('unit_price', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('line_total', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.CheckConstraint('line_total = unit_price * quantity', name=op.f('ck_order_items_line_total_consistent')),
    sa.CheckConstraint('quantity > 0', name=op.f('ck_order_items_quantity_positive')),
    sa.CheckConstraint('unit_price >= 0', name=op.f('ck_order_items_unit_price_non_negative')),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_order_items_order_id_orders'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_order_items_product_id_products'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_order_items')),
    sa.UniqueConstraint('order_id', 'product_id', name='uq_order_items_order_product')
    )
    op.create_index('ix_order_items_product_id', 'order_items', ['product_id'], unique=False)
    op.create_table('order_status_history',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=False),
    sa.Column('from_status', sa.Enum('pending_confirmation', 'confirmed', 'preparing', 'ready', 'delivered', 'cancelled', name='order_status'), nullable=True),
    sa.Column('to_status', sa.Enum('pending_confirmation', 'confirmed', 'preparing', 'ready', 'delivered', 'cancelled', name='order_status'), nullable=False),
    sa.Column('note', sa.String(length=500), nullable=True),
    sa.Column('changed_by_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['changed_by_id'], ['users.id'], name=op.f('fk_order_status_history_changed_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_order_status_history_order_id_orders'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_order_status_history'))
    )
    op.create_index('ix_order_status_history_order_id', 'order_status_history', ['order_id', 'created_at'], unique=False)
    op.create_table('payment_proofs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=False),
    sa.Column('storage_path', sa.String(length=255), nullable=False),
    sa.Column('original_filename', sa.String(length=150), nullable=True),
    sa.Column('mime_type', sa.String(length=50), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('declared_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('status', sa.Enum('pending_verification', 'approved', 'rejected', 'refunded', name='proof_status'), nullable=False),
    sa.Column('reviewed_by_id', sa.UUID(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('rejection_reason', sa.String(length=500), nullable=True),
    sa.Column('review_note', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("mime_type IN ('image/jpeg','image/png','image/webp')", name=op.f('ck_payment_proofs_mime_type_allowed')),
    sa.CheckConstraint("status <> 'rejected' OR (rejection_reason IS NOT NULL AND length(trim(rejection_reason)) > 0)", name=op.f('ck_payment_proofs_rejection_reason_required')),
    sa.CheckConstraint("status = 'pending_verification' OR (reviewed_at IS NOT NULL)", name=op.f('ck_payment_proofs_reviewed_when_not_pending')),
    sa.CheckConstraint('declared_amount > 0', name=op.f('ck_payment_proofs_declared_amount_positive')),
    sa.CheckConstraint('size_bytes > 0', name=op.f('ck_payment_proofs_size_positive')),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_payment_proofs_order_id_orders'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['reviewed_by_id'], ['users.id'], name=op.f('fk_payment_proofs_reviewed_by_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_payment_proofs')),
    sa.UniqueConstraint('storage_path', name=op.f('uq_payment_proofs_storage_path'))
    )
    op.create_index('ix_payment_proofs_order_id', 'payment_proofs', ['order_id'], unique=False)
    op.create_index('ix_payment_proofs_status', 'payment_proofs', ['status', 'created_at'], unique=False)
    op.create_table('payments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=False),
    sa.Column('method', sa.Enum('cash', 'transfer', 'credit', name='payment_method'), nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('status', sa.Enum('completed', 'refunded', name='payment_record_status'), nullable=False),
    sa.Column('proof_id', sa.UUID(), nullable=True),
    sa.Column('cash_session_id', sa.UUID(), nullable=True),
    sa.Column('reference', sa.String(length=120), nullable=True),
    sa.Column('note', sa.String(length=500), nullable=True),
    sa.Column('received_by_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('refunded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('refunded_by_id', sa.UUID(), nullable=True),
    sa.Column('refund_reason', sa.String(length=500), nullable=True),
    sa.CheckConstraint("method <> 'cash' OR cash_session_id IS NOT NULL", name=op.f('ck_payments_cash_requires_session')),
    sa.CheckConstraint("method IN ('cash','transfer')", name=op.f('ck_payments_method_is_money')),
    sa.CheckConstraint("proof_id IS NULL OR method = 'transfer'", name=op.f('ck_payments_proof_only_transfer')),
    sa.CheckConstraint("status <> 'refunded' OR (refunded_at IS NOT NULL AND refund_reason IS NOT NULL)", name=op.f('ck_payments_refund_fields')),
    sa.CheckConstraint('amount > 0', name=op.f('ck_payments_amount_positive')),
    sa.ForeignKeyConstraint(['cash_session_id'], ['cash_sessions.id'], name=op.f('fk_payments_cash_session_id_cash_sessions'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_payments_order_id_orders'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['proof_id'], ['payment_proofs.id'], name=op.f('fk_payments_proof_id_payment_proofs'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['received_by_id'], ['users.id'], name=op.f('fk_payments_received_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['refunded_by_id'], ['users.id'], name=op.f('fk_payments_refunded_by_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_payments')),
    sa.UniqueConstraint('proof_id', name=op.f('uq_payments_proof_id'))
    )
    op.create_index('ix_payments_created_at', 'payments', ['created_at'], unique=False)
    op.create_index('ix_payments_order_id', 'payments', ['order_id'], unique=False)
    op.create_table('cash_movements',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('session_id', sa.UUID(), nullable=False),
    sa.Column('type', sa.Enum('sale_cash', 'refund_cash', 'expense', 'withdrawal', 'deposit', 'adjustment', name='cash_movement_type'), nullable=False),
    sa.Column('direction', sa.Enum('in', 'out', name='movement_direction'), nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('reason', sa.String(length=500), nullable=True),
    sa.Column('payment_id', sa.UUID(), nullable=True),
    sa.Column('refund_payment_id', sa.UUID(), nullable=True),
    sa.Column('created_by_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(type IN ('sale_cash','deposit') AND direction = 'in') OR (type IN ('refund_cash','expense','withdrawal') AND direction = 'out') OR type = 'adjustment'", name=op.f('ck_cash_movements_direction_matches_type')),
    sa.CheckConstraint("type <> 'refund_cash' OR refund_payment_id IS NOT NULL", name=op.f('ck_cash_movements_refund_has_payment')),
    sa.CheckConstraint("type <> 'sale_cash' OR payment_id IS NOT NULL", name=op.f('ck_cash_movements_sale_has_payment')),
    sa.CheckConstraint("type IN ('sale_cash','refund_cash') OR (reason IS NOT NULL AND length(trim(reason)) > 0)", name=op.f('ck_cash_movements_manual_requires_reason')),
    sa.CheckConstraint('amount > 0', name=op.f('ck_cash_movements_amount_positive')),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_cash_movements_created_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['payment_id'], ['payments.id'], name=op.f('fk_cash_movements_payment_id_payments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['refund_payment_id'], ['payments.id'], name=op.f('fk_cash_movements_refund_payment_id_payments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['session_id'], ['cash_sessions.id'], name=op.f('fk_cash_movements_session_id_cash_sessions'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cash_movements')),
    sa.UniqueConstraint('payment_id', name=op.f('uq_cash_movements_payment_id')),
    sa.UniqueConstraint('refund_payment_id', name=op.f('uq_cash_movements_refund_payment_id'))
    )
    op.create_index('ix_cash_movements_session_id', 'cash_movements', ['session_id', 'created_at'], unique=False)


    # ---- singleton settings row
    op.execute(
        "INSERT INTO business_settings (id, business_name, timezone) "
        "VALUES (1, 'Fritos de la Iglesia', 'America/Bogota')"
    )

    # ---- closed cash sessions are immutable (defence in depth; services also check)
    op.execute(
        """
        CREATE FUNCTION cash_session_immutable() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status = 'closed' THEN
                    RAISE EXCEPTION 'cash session % is closed and immutable', OLD.id USING ERRCODE = 'check_violation';
                END IF;
                RETURN OLD;
            END IF;
            IF OLD.status = 'closed' THEN
                RAISE EXCEPTION 'cash session % is closed and immutable', OLD.id USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_cash_sessions_immutable BEFORE UPDATE OR DELETE ON cash_sessions "
        "FOR EACH ROW EXECUTE FUNCTION cash_session_immutable()"
    )
    op.execute(
        """
        CREATE FUNCTION cash_child_requires_open_session() RETURNS trigger AS $$
        DECLARE
            s_status cash_session_status;
            s_id uuid;
        BEGIN
            IF TG_OP = 'DELETE' THEN s_id := OLD.session_id; ELSE s_id := NEW.session_id; END IF;
            SELECT status INTO s_status FROM cash_sessions WHERE id = s_id;
            IF s_status = 'closed' THEN
                RAISE EXCEPTION 'cash session % is closed and immutable', s_id USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.session_id <> NEW.session_id THEN
                RAISE EXCEPTION 'cannot move rows between cash sessions' USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_cash_movements_open_session BEFORE INSERT OR UPDATE OR DELETE ON cash_movements "
        "FOR EACH ROW EXECUTE FUNCTION cash_child_requires_open_session()"
    )
    op.execute(
        "CREATE TRIGGER trg_cash_count_lines_open_session BEFORE INSERT OR UPDATE OR DELETE ON cash_count_lines "
        "FOR EACH ROW EXECUTE FUNCTION cash_child_requires_open_session()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_cash_count_lines_open_session ON cash_count_lines")
    op.execute("DROP TRIGGER IF EXISTS trg_cash_movements_open_session ON cash_movements")
    op.execute("DROP TRIGGER IF EXISTS trg_cash_sessions_immutable ON cash_sessions")
    op.execute("DROP FUNCTION IF EXISTS cash_child_requires_open_session()")
    op.execute("DROP FUNCTION IF EXISTS cash_session_immutable()")
    op.drop_index('ix_cash_movements_session_id', table_name='cash_movements')
    op.drop_table('cash_movements')
    op.drop_index('ix_payments_order_id', table_name='payments')
    op.drop_index('ix_payments_created_at', table_name='payments')
    op.drop_table('payments')
    op.drop_index('ix_payment_proofs_status', table_name='payment_proofs')
    op.drop_index('ix_payment_proofs_order_id', table_name='payment_proofs')
    op.drop_table('payment_proofs')
    op.drop_index('ix_order_status_history_order_id', table_name='order_status_history')
    op.drop_table('order_status_history')
    op.drop_index('ix_order_items_product_id', table_name='order_items')
    op.drop_table('order_items')
    op.drop_index('ix_debt_writeoffs_order_id', table_name='debt_writeoffs')
    op.drop_table('debt_writeoffs')
    op.drop_table('cash_count_lines')
    op.drop_index('ix_refresh_tokens_user_id', table_name='refresh_tokens')
    op.drop_index('ix_refresh_tokens_family_id', table_name='refresh_tokens')
    op.drop_table('refresh_tokens')
    op.drop_index('ix_orders_status', table_name='orders')
    op.drop_index('ix_orders_payment_status', table_name='orders')
    op.drop_index('ix_orders_customer_phone_created', table_name='orders')
    op.drop_index('ix_orders_customer_id', table_name='orders')
    op.drop_index('ix_orders_created_at', table_name='orders')
    op.drop_table('orders')
    op.drop_index('uq_cash_sessions_single_open', table_name='cash_sessions', postgresql_where=sa.text("status = 'open'"))
    op.drop_index('ix_cash_sessions_opened_at', table_name='cash_sessions')
    op.drop_table('cash_sessions')
    op.drop_table('business_settings')
    op.drop_index('ix_audit_logs_entity', table_name='audit_logs')
    op.drop_index('ix_audit_logs_created_at', table_name='audit_logs')
    op.drop_index('ix_audit_logs_action', table_name='audit_logs')
    op.drop_table('audit_logs')
    op.drop_table('users')
    op.drop_index('ix_products_active_sort', table_name='products')
    op.drop_table('products')
    op.drop_index('ix_idempotency_keys_created_at', table_name='idempotency_keys')
    op.drop_table('idempotency_keys')
    op.drop_index('uq_customers_phone_normalized', table_name='customers', postgresql_where=sa.text('phone_normalized IS NOT NULL'))
    op.drop_index('ix_customers_name', table_name='customers')
    op.drop_table('customers')
    for enum_name in (
        "cash_movement_type",
        "movement_direction",
        "denomination_kind",
        "cash_session_status",
        "payment_record_status",
        "proof_status",
        "credit_status",
        "payment_status",
        "order_status",
        "payment_method",
        "user_role",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
