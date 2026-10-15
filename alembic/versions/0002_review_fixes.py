"""review fixes: unverified web customers, credit 'not_needed', proof sha256 index, frozen cash
transfer totals, hashed idempotency keys

  * customers.is_verified: anonymous web orders create unverified customer rows; the unique phone
    index now applies to verified customers only. Backfill: a legacy customer is verified when an
    admin created it (audit 'customer_created') or it has a counter (source='admin') order.
  * credit_status gains 'not_needed' (credit requested, order fully paid before authorization).
  * index on payment_proofs.sha256 (duplicate proof detection).
  * cash_sessions.transfer_income / transfer_payments_count, frozen at close. Backfilled for closed
    sessions from completed transfer payments (refunded ones cannot be told apart any more).
  * idempotency_keys.key now stores SHA-256(key). Existing keys are hashed in place; rows whose
    stored response contains a plaintext tracking_token are deleted.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-12
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CREDIT_OLD = "(payment_method = 'credit') = (credit_status <> 'none') OR credit_status IN ('authorized','rejected')"
_CREDIT_NEW = (
    "(payment_method = 'credit') = (credit_status <> 'none') "
    "OR credit_status IN ('authorized','rejected','not_needed')"
)


def upgrade() -> None:
    # ---- enum value must be committed before it can be used (constraint below)
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE credit_status ADD VALUE IF NOT EXISTS 'not_needed'")

    op.drop_constraint(op.f("ck_orders_credit_status_consistent"), "orders", type_="check")
    op.create_check_constraint(op.f("ck_orders_credit_status_consistent"), "orders", _CREDIT_NEW)

    # ---- customers
    op.add_column(
        "customers", sa.Column("is_verified", sa.Boolean(), server_default=sa.text("true"), nullable=False)
    )
    op.execute(
        """
        UPDATE customers c SET is_verified = false
        WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id AND o.source = 'admin')
          AND NOT EXISTS (SELECT 1 FROM audit_logs a
                          WHERE a.action = 'customer_created' AND a.entity_id = c.id)
          AND EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id)
        """
    )
    op.drop_index("uq_customers_phone_normalized", table_name="customers")
    op.create_index(
        "uq_customers_phone_normalized", "customers", ["phone_normalized"], unique=True,
        postgresql_where=sa.text("phone_normalized IS NOT NULL AND is_verified"),
    )
    op.create_index("ix_customers_phone_normalized", "customers", ["phone_normalized"])

    # ---- proofs
    op.create_index("ix_payment_proofs_sha256", "payment_proofs", ["sha256"])

    # ---- cash sessions (the immutability trigger forbids updating closed rows: disable it briefly)
    op.add_column("cash_sessions", sa.Column("transfer_income", sa.Numeric(12, 2), nullable=True))
    op.add_column("cash_sessions", sa.Column("transfer_payments_count", sa.Integer(), nullable=True))
    op.execute("ALTER TABLE cash_sessions DISABLE TRIGGER USER")
    op.execute(
        """
        UPDATE cash_sessions s SET
          transfer_income = (SELECT COALESCE(SUM(p.amount), 0) FROM payments p
                             WHERE p.method = 'transfer' AND p.status = 'completed'
                               AND p.created_at >= s.opened_at AND p.created_at <= s.closed_at),
          transfer_payments_count = (SELECT COUNT(*) FROM payments p
                             WHERE p.method = 'transfer' AND p.status = 'completed'
                               AND p.created_at >= s.opened_at AND p.created_at <= s.closed_at)
        WHERE s.status = 'closed'
        """
    )
    op.execute("ALTER TABLE cash_sessions ENABLE TRIGGER USER")

    # ---- idempotency keys: never keep raw keys or plaintext tracking tokens
    op.execute("DELETE FROM idempotency_keys WHERE response_body ? 'tracking_token'")
    op.execute("UPDATE idempotency_keys SET key = encode(sha256(convert_to(key, 'UTF8')), 'hex')")


def downgrade() -> None:
    # Hashed idempotency keys cannot be un-hashed: drop them (clients just lose replay for old keys).
    op.execute("DELETE FROM idempotency_keys")

    op.execute("ALTER TABLE cash_sessions DISABLE TRIGGER USER")
    op.drop_column("cash_sessions", "transfer_payments_count")
    op.drop_column("cash_sessions", "transfer_income")
    op.execute("ALTER TABLE cash_sessions ENABLE TRIGGER USER")

    op.drop_index("ix_payment_proofs_sha256", table_name="payment_proofs")

    op.drop_index("ix_customers_phone_normalized", table_name="customers")
    op.drop_index("uq_customers_phone_normalized", table_name="customers")
    # Unverified duplicates would violate the old unique index: detach their phone.
    op.execute(
        """
        UPDATE customers c SET phone_normalized = NULL
        WHERE NOT c.is_verified AND c.phone_normalized IS NOT NULL
          AND EXISTS (SELECT 1 FROM customers v WHERE v.phone_normalized = c.phone_normalized AND v.id <> c.id
                      AND (v.is_verified OR v.created_at < c.created_at OR (v.created_at = c.created_at AND v.id < c.id)))
        """
    )
    op.create_index(
        "uq_customers_phone_normalized", "customers", ["phone_normalized"], unique=True,
        postgresql_where=sa.text("phone_normalized IS NOT NULL"),
    )
    op.drop_column("customers", "is_verified")

    # ---- credit_status: map 'not_needed' (always balance 0) to 'authorized', then rebuild the enum
    op.drop_constraint(op.f("ck_orders_credit_status_consistent"), "orders", type_="check")
    op.execute("UPDATE orders SET credit_status = 'authorized' WHERE credit_status = 'not_needed'")
    op.execute("ALTER TYPE credit_status RENAME TO credit_status_old")
    op.execute("CREATE TYPE credit_status AS ENUM ('none', 'pending_authorization', 'authorized', 'rejected')")
    op.execute("ALTER TABLE orders ALTER COLUMN credit_status DROP DEFAULT")
    op.execute(
        "ALTER TABLE orders ALTER COLUMN credit_status TYPE credit_status USING credit_status::text::credit_status"
    )
    op.execute("DROP TYPE credit_status_old")
    op.create_check_constraint(op.f("ck_orders_credit_status_consistent"), "orders", _CREDIT_OLD)
