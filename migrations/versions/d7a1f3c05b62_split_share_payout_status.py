"""split bill share payout_status

Tracks the organizer's payout for a paid share separately from the
participant's payment, so a failed/bounced payout is visible and retryable.

Revision ID: d7a1f3c05b62
Revises: c4d8e2a91f37
Create Date: 2026-09-29

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd7a1f3c05b62'
down_revision: Union[str, None] = 'c4d8e2a91f37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

payout_status = sa.Enum('NOT_STARTED', 'PENDING', 'COMPLETED', 'FAILED', name='split_bill_share_payout_status')


def upgrade() -> None:
    payout_status.create(op.get_bind(), checkfirst=True)
    op.add_column(
        'split_bill_shares',
        sa.Column('payout_status', payout_status, nullable=False, server_default='NOT_STARTED'),
    )
    # Shares paid before this migration had their payout fired under the old
    # fire-and-forget code, which treated that as done - mirror that rather
    # than leave them looking stuck in PENDING forever.
    op.execute("UPDATE split_bill_shares SET payout_status = 'COMPLETED' WHERE status = 'PAID'")
    op.alter_column('split_bill_shares', 'payout_status', server_default=None)


def downgrade() -> None:
    op.drop_column('split_bill_shares', 'payout_status')
    payout_status.drop(op.get_bind(), checkfirst=True)
