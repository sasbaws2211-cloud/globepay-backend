"""lite cards only - drop standard-card KYC, top-ups and withdrawals

GlobePay issues Bitnob LITE cards only (loaded once, never topped up or
withdrawn from, no identity check), so the standard-card tables and the
virtual_cards.card_type column go.

Revision ID: 9d4e2b7c1a05
Revises: 1bf02dd5cedb
Create Date: 2026-09-30 03:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # SQLModel column types in the downgrade


# revision identifiers, used by Alembic.
revision: str = '9d4e2b7c1a05'
down_revision: Union[str, None] = '1bf02dd5cedb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(op.f('ix_card_withdrawals_payout_reference'), table_name='card_withdrawals')
    op.drop_index(op.f('ix_card_withdrawals_card_id'), table_name='card_withdrawals')
    op.drop_index(op.f('ix_card_withdrawals_bitnob_reference'), table_name='card_withdrawals')
    op.drop_table('card_withdrawals')
    op.drop_index(op.f('ix_card_fundings_payment_reference'), table_name='card_fundings')
    op.drop_index(op.f('ix_card_fundings_card_id'), table_name='card_fundings')
    op.drop_table('card_fundings')
    op.drop_index(op.f('ix_card_kyc_user_id'), table_name='card_kyc')
    op.drop_index(op.f('ix_card_kyc_bitnob_customer_id'), table_name='card_kyc')
    op.drop_table('card_kyc')
    op.drop_column('virtual_cards', 'card_type')
    for enum_name in ('card_withdrawal_status', 'card_funding_status', 'card_kyc_status', 'virtual_card_type'):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    card_type = sa.Enum('LITE', 'STANDARD', name='virtual_card_type')
    card_type.create(op.get_bind(), checkfirst=True)
    op.add_column('virtual_cards', sa.Column('card_type', card_type, nullable=False, server_default='LITE'))
    op.alter_column('virtual_cards', 'card_type', server_default=None)

    op.create_table('card_kyc',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('bitnob_customer_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('status', sa.Enum('INITIATED', 'PENDING', 'APPROVED', 'REJECTED', name='card_kyc_status'), nullable=False),
    sa.Column('completion_link', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('rejection_details', sa.JSON(), nullable=True),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_card_kyc_bitnob_customer_id'), 'card_kyc', ['bitnob_customer_id'], unique=False)
    op.create_index(op.f('ix_card_kyc_user_id'), 'card_kyc', ['user_id'], unique=True)
    op.create_table('card_fundings',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('card_id', sa.Uuid(), nullable=False),
    sa.Column('amount_ghs', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('amount_usd', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('fee_ghs', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('status', sa.Enum('PENDING_PAYMENT', 'COMPLETED', 'FAILED', 'DELIVERY_FAILED', 'PROCESSING', 'REFUND_PENDING', 'REFUNDED', name='card_funding_status'), nullable=False),
    sa.Column('payment_reference', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('failure_reason', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('retry_count', sa.Integer(), nullable=False),
    sa.Column('refund_reference', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('refunded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['card_id'], ['virtual_cards.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_card_fundings_card_id'), 'card_fundings', ['card_id'], unique=False)
    op.create_index(op.f('ix_card_fundings_payment_reference'), 'card_fundings', ['payment_reference'], unique=False)
    op.create_table('card_withdrawals',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('card_id', sa.Uuid(), nullable=False),
    sa.Column('amount_usd', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('amount_ghs', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('status', sa.Enum('CARD_PENDING', 'CARD_FAILED', 'PAYOUT_PENDING', 'PAYOUT_FAILED', 'COMPLETED', name='card_withdrawal_status'), nullable=False),
    sa.Column('bitnob_reference', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('payout_reference', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('payout_attempts', sa.Integer(), nullable=False),
    sa.Column('failure_reason', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['card_id'], ['virtual_cards.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_card_withdrawals_bitnob_reference'), 'card_withdrawals', ['bitnob_reference'], unique=False)
    op.create_index(op.f('ix_card_withdrawals_card_id'), 'card_withdrawals', ['card_id'], unique=False)
    op.create_index(op.f('ix_card_withdrawals_payout_reference'), 'card_withdrawals', ['payout_reference'], unique=False)
