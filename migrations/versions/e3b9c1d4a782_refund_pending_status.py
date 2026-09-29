"""REFUND_PENDING status for cards, card top-ups and cross-border transfers

A refund used to be marked REFUNDED the moment Paystack accepted the
request. It now sits in REFUND_PENDING until Paystack reports the refund
processed (or failed, which sends it back to DELIVERY_FAILED).

Revision ID: e3b9c1d4a782
Revises: d7a1f3c05b62
Create Date: 2026-09-29

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e3b9c1d4a782'
down_revision: Union[str, None] = 'd7a1f3c05b62'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ENUM_TYPES = ("virtual_card_status", "card_funding_status", "crossborder_status")
TABLES = ("virtual_cards", "card_fundings", "crossborder_transfers")


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE can't run inside a transaction block on older Postgres.
    with op.get_context().autocommit_block():
        for enum_type in ENUM_TYPES:
            op.execute(f"ALTER TYPE {enum_type} ADD VALUE IF NOT EXISTS 'REFUND_PENDING'")


def downgrade() -> None:
    # Postgres can't drop an enum value. Map in-flight refunds to what the old
    # code would have recorded - never back to a refundable state, which could
    # let a second refund be issued for the same charge.
    for table in TABLES:
        op.execute(f"UPDATE {table} SET status = 'REFUNDED' WHERE status = 'REFUND_PENDING'")
