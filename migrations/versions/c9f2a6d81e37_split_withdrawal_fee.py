"""split-bill withdrawals record their platform fee (waived for vaults)

Revision ID: c9f2a6d81e37
Revises: b5e8c3a17d42
Create Date: 2026-09-30 05:40:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9f2a6d81e37'
down_revision: Union[str, None] = 'b5e8c3a17d42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'split_bill_withdrawals',
        sa.Column('fee', sa.Numeric(precision=14, scale=2), nullable=False, server_default='0'),
    )
    # Withdrawals made before this change all had the fee taken: it's what the
    # paying participants' shares came to, minus what the organizer received.
    op.execute("""
        UPDATE split_bill_withdrawals w
        SET fee = paid.gross - w.amount
        FROM (
            SELECT s.split_bill_id, SUM(s.gross_amount) AS gross
            FROM split_bill_shares s
            JOIN split_bills b ON b.id = s.split_bill_id
            WHERE s.status = 'PAID' AND s.user_id <> b.organizer_id
            GROUP BY s.split_bill_id
        ) paid
        WHERE paid.split_bill_id = w.split_bill_id
    """)


def downgrade() -> None:
    op.drop_column('split_bill_withdrawals', 'fee')
