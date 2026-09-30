import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from sqlmodel import Field, SQLModel

from src.common.db_types import named_enum_column, tz_aware_column


class SplitBillStatus(StrEnum):
    OPEN = "open"
    # Every share paid (or the organizer closed it early). For a collecting
    # bill this means "ready to withdraw"; for an older per-share bill, done.
    SETTLED = "settled"
    CANCELLED = "cancelled"  # organizer voided it before anyone paid - see cancel_split_bill
    WITHDRAWN = "withdrawn"  # collecting bill: the organizer's withdrawal completed


class ShareStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"  # the participant's charge succeeded - says nothing about the organizer's payout
    # The organizer closed the bill before this person paid - they no longer owe it.
    CANCELLED = "cancelled"


class SharePayoutStatus(StrEnum):
    """The organizer's payout for a PAID share on an older, per-share bill
    (collects_funds False). Collecting bills leave this NOT_STARTED and pay
    out once, through SplitBillWithdrawal."""

    NOT_STARTED = "not_started"
    PENDING = "pending"  # Paystack accepted the request; waiting on the transfer.* webhook
    COMPLETED = "completed"
    FAILED = "failed"  # organizer can retry via retry_share_payout


class SplitBill(SQLModel, table=True):
    """A one-off shared expense - one organizer paid upfront and collects
    each participant's share via real Paystack charges.

    collects_funds (every bill created from 2026-09-30): paid shares are held
    on the bill, and once it's settled the organizer withdraws the total in
    one go - to mobile money or into one of their vaults. Older bills
    (collects_funds False) paid each share out to the organizer's mobile
    money the moment it was paid."""

    __tablename__ = "split_bills"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    organizer_id: uuid.UUID = Field(foreign_key="users.id", index=True)

    title: str
    total_amount: Decimal = Field(max_digits=14, decimal_places=2)
    status: SplitBillStatus = Field(
        default=SplitBillStatus.OPEN, sa_column=named_enum_column(SplitBillStatus, "split_bill_status")
    )
    collects_funds: bool = Field(default=True)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=tz_aware_column())


class WithdrawalDestination(StrEnum):
    MOMO = "momo"  # Paystack transfer to the organizer's saved mobile money
    VAULT = "vault"  # credited straight into one of the organizer's vaults - no transfer


class SplitWithdrawalStatus(StrEnum):
    PENDING = "pending"  # mobile money transfer requested; settles by webhook or reconcile sweep
    COMPLETED = "completed"
    FAILED = "failed"  # the organizer can try again, to either destination


class SplitBillWithdrawal(SQLModel, table=True):
    """The organizer taking a settled bill's collected money out - one per
    bill (unique), so a double tap can't withdraw twice. A failed attempt is
    retried on this same row."""

    __tablename__ = "split_bill_withdrawals"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    split_bill_id: uuid.UUID = Field(foreign_key="split_bills.id", unique=True, index=True)
    destination: WithdrawalDestination = Field(
        sa_column=named_enum_column(WithdrawalDestination, "split_withdrawal_destination")
    )
    vault_id: uuid.UUID | None = Field(default=None, foreign_key="vaults.id")
    # What the organizer receives. Mobile money: the paid shares minus the
    # platform fee. Vault: the full amount paid - the fee is waived to
    # encourage saving.
    amount: Decimal = Field(max_digits=14, decimal_places=2)
    fee: Decimal = Field(default=Decimal("0.00"), max_digits=14, decimal_places=2)  # platform fee taken
    status: SplitWithdrawalStatus = Field(
        default=SplitWithdrawalStatus.PENDING,
        sa_column=named_enum_column(SplitWithdrawalStatus, "split_withdrawal_status"),
    )
    payout_reference: str | None = Field(default=None, index=True)
    attempts: int = Field(default=0)
    failure_reason: str | None = Field(default=None)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=tz_aware_column())
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=tz_aware_column())


class SplitBillShare(SQLModel, table=True):
    __tablename__ = "split_bill_shares"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    split_bill_id: uuid.UUID = Field(foreign_key="split_bills.id", index=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)

    gross_amount: Decimal = Field(max_digits=14, decimal_places=2)
    platform_fee: Decimal = Field(max_digits=14, decimal_places=2)
    net_amount: Decimal = Field(max_digits=14, decimal_places=2)  # what the organizer actually receives

    status: ShareStatus = Field(
        default=ShareStatus.PENDING, sa_column=named_enum_column(ShareStatus, "split_bill_share_status")
    )
    payment_reference: str | None = Field(default=None, index=True)
    payout_reference: str | None = Field(default=None)
    payout_status: SharePayoutStatus = Field(
        default=SharePayoutStatus.NOT_STARTED,
        sa_column=named_enum_column(SharePayoutStatus, "split_bill_share_payout_status"),
    )
    # Set when a payment landed after the share was cancelled (bill closed or
    # cancelled while this person's checkout was still open) and was refunded.
    # Also stops a repeat webhook from refunding twice.
    refund_reference: str | None = Field(default=None)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=tz_aware_column())
    paid_at: datetime | None = Field(default=None, sa_column=tz_aware_column(nullable=True))
