"""split bills: collect then withdraw (mobile money or vault)

New bills hold paid shares until the organizer withdraws the total in one
go. Existing bills keep paying each share out as it's paid
(collects_funds = false).

Revision ID: b5e8c3a17d42
Revises: 9d4e2b7c1a05
Create Date: 2026-09-30 05:10:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 - SQLModel column types


# revision identifiers, used by Alembic.
revision: str = 'b5e8c3a17d42'
down_revision: Union[str, None] = '9d4e2b7c1a05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enum values are stored by NAME. ADD VALUE is fine inside a transaction on
    # Postgres 12+ as long as the new value isn't used in the same one.
    op.execute("ALTER TYPE split_bill_status ADD VALUE IF NOT EXISTS 'WITHDRAWN'")
    op.execute("ALTER TYPE split_bill_share_status ADD VALUE IF NOT EXISTS 'CANCELLED'")

    op.add_column('split_bills', sa.Column('collects_funds', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('split_bill_shares', sa.Column('refund_reference', sqlmodel.sql.sqltypes.AutoString(), nullable=True))

    op.create_table('split_bill_withdrawals',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('split_bill_id', sa.Uuid(), nullable=False),
    sa.Column('destination', sa.Enum('MOMO', 'VAULT', name='split_withdrawal_destination'), nullable=False),
    sa.Column('vault_id', sa.Uuid(), nullable=True),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('status', sa.Enum('PENDING', 'COMPLETED', 'FAILED', name='split_withdrawal_status'), nullable=False),
    sa.Column('payout_reference', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('failure_reason', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['split_bill_id'], ['split_bills.id'], ),
    sa.ForeignKeyConstraint(['vault_id'], ['vaults.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_split_bill_withdrawals_split_bill_id'), 'split_bill_withdrawals', ['split_bill_id'], unique=True)
    op.create_index(op.f('ix_split_bill_withdrawals_payout_reference'), 'split_bill_withdrawals', ['payout_reference'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_split_bill_withdrawals_payout_reference'), table_name='split_bill_withdrawals')
    op.drop_index(op.f('ix_split_bill_withdrawals_split_bill_id'), table_name='split_bill_withdrawals')
    op.drop_table('split_bill_withdrawals')
    for enum_name in ('split_withdrawal_status', 'split_withdrawal_destination'):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
    op.drop_column('split_bill_shares', 'refund_reference')
    op.drop_column('split_bills', 'collects_funds')
    # Postgres can't drop enum values. Map rows using them back to the closest
    # older meaning; the unused WITHDRAWN / CANCELLED labels stay on the types.
    op.execute("UPDATE split_bills SET status = 'SETTLED' WHERE status = 'WITHDRAWN'")
    op.execute("UPDATE split_bill_shares SET status = 'PENDING' WHERE status = 'CANCELLED'")
