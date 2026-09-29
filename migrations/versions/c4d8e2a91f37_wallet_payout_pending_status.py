"""wallet transfer PAYOUT_PENDING status

A wallet transfer is no longer marked COMPLETED the moment Paystack accepts
the payout request - it sits in PAYOUT_PENDING until the transfer.success
webhook confirms delivery.

Revision ID: c4d8e2a91f37
Revises: 41f0c583240b
Create Date: 2026-09-29

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c4d8e2a91f37'
down_revision: Union[str, None] = '41f0c583240b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE can't run inside a transaction block on older Postgres.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE transferstatus ADD VALUE IF NOT EXISTS 'PAYOUT_PENDING'")


def downgrade() -> None:
    # Postgres can't drop an enum value. Map in-flight payouts to what the old
    # code would have recorded (COMPLETED) - never back to a claimable state,
    # which would let the recipient trigger a second payout.
    op.execute("UPDATE wallet_transfers SET status = 'COMPLETED' WHERE status = 'PAYOUT_PENDING'")
