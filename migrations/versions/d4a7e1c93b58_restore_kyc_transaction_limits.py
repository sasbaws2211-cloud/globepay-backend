"""restore KYC-tier transaction limits (users.kyc_tier + transaction_volume_logs)

The original limits were lost (source deleted, columns/table dropped by the
2026-09-30 migration squash). Tiers and amounts are restored from the
surviving bytecode - see src/common/kyc_limits.py.

Backfill:
  - users.kyc_tier: PHONE_VERIFIED for anyone who already verified their
    phone, else UNVERIFIED. Nobody starts at ID_VERIFIED - an admin sets it.
  - transaction_volume_logs: the last 30 days of confirmed payments INTO
    GlobePay, so today's limits see recent volume instead of starting from
    zero. Money moved inside GlobePay (round-ups, referral bonuses, split
    bills withdrawn into a vault) is excluded, as it is going forward.

Revision ID: d4a7e1c93b58
Revises: c9f2a6d81e37
Create Date: 2026-09-30 06:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'd4a7e1c93b58'
down_revision: Union[str, None] = 'c9f2a6d81e37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    kyc_tier = sa.Enum('UNVERIFIED', 'PHONE_VERIFIED', 'ID_VERIFIED', name='kyc_tier')
    kyc_tier.create(op.get_bind(), checkfirst=True)
    op.add_column('users', sa.Column('kyc_tier', kyc_tier, nullable=False, server_default='UNVERIFIED'))
    op.execute("UPDATE users SET kyc_tier = 'PHONE_VERIFIED' WHERE is_phone_verified")

    op.create_table('transaction_volume_logs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_transaction_volume_logs_user_id'), 'transaction_volume_logs', ['user_id'], unique=False)

    since = "now() - interval '30 days'"
    op.execute(f"""
        INSERT INTO transaction_volume_logs (id, user_id, amount, source, created_at)
        SELECT gen_random_uuid(), sender_id, gross_amount + roundup_amount, 'wallet_transfer', created_at
        FROM wallet_transfers
        WHERE status::text NOT IN ('PENDING_PAYMENT', 'FAILED') AND created_at >= {since}
    """)
    op.execute(f"""
        INSERT INTO transaction_volume_logs (id, user_id, amount, source, created_at)
        SELECT gen_random_uuid(), v.owner_id, c.amount, 'vault_contribution', COALESCE(c.paid_at, c.created_at)
        FROM vault_contributions c JOIN vaults v ON v.id = c.vault_id
        WHERE c.status::text = 'PAID' AND COALESCE(c.paid_at, c.created_at) >= {since}
          AND c.payment_reference NOT LIKE 'split-%'
          AND c.payment_reference NOT LIKE 'referral-bonus-%'
          AND c.payment_reference NOT LIKE '%-roundup'
    """)
    op.execute(f"""
        INSERT INTO transaction_volume_logs (id, user_id, amount, source, created_at)
        SELECT gen_random_uuid(), sender_id, source_amount, 'crossborder_transfer', created_at
        FROM crossborder_transfers
        WHERE status::text NOT IN ('PENDING_PAYMENT', 'FAILED') AND created_at >= {since}
    """)
    op.execute(f"""
        INSERT INTO transaction_volume_logs (id, user_id, amount, source, created_at)
        SELECT gen_random_uuid(), user_id, initial_funding_ghs + fee_ghs, 'card_creation', created_at
        FROM virtual_cards
        WHERE status::text NOT IN ('PENDING_PAYMENT', 'FAILED') AND created_at >= {since}
    """)
    op.execute(f"""
        INSERT INTO transaction_volume_logs (id, user_id, amount, source, created_at)
        SELECT gen_random_uuid(), s.user_id, s.gross_amount, 'splitbill_share', s.paid_at
        FROM split_bill_shares s JOIN split_bills b ON b.id = s.split_bill_id
        WHERE s.status::text = 'PAID' AND s.user_id <> b.organizer_id AND s.paid_at >= {since}
    """)


def downgrade() -> None:
    op.drop_index(op.f('ix_transaction_volume_logs_user_id'), table_name='transaction_volume_logs')
    op.drop_table('transaction_volume_logs')
    op.drop_column('users', 'kyc_tier')
    sa.Enum(name='kyc_tier').drop(op.get_bind(), checkfirst=True)
