import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from sqlmodel import Field, SQLModel

from src.common.db_types import named_enum_column, tz_aware_column


class SplitBillStatus(StrEnum):
    OPEN = "open"
    SETTLED = "settled"
    CANCELLED = "cancelled"  # organizer voided it before anyone paid - see cancel_split_bill


class ShareStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"  # the participant's charge succeeded - says nothing about the organizer's payout


class SharePayoutStatus(StrEnum):
    """The organizer's payout for a PAID share, tracked separately so a
    payout that fails or bounces can't hide behind the participant having paid."""

    NOT_STARTED = "not_started"
    PENDING = "pending"  # Paystack accepted the request; waiting on the transfer.* webhook
    COMPLETED = "completed"
    FAILED = "failed"  # organizer can retry via retry_share_payout


class SplitBill(SQLModel, table=True):
    """A one-off shared expense - one organizer paid upfront and collects
    each participant's share directly, via real Paystack transactions, same
    non-custodial pattern as wallet-to-wallet transfers."""

    __tablename__ = "split_bills"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    organizer_id: uuid.UUID = Field(foreign_key="users.id", index=True)

    title: str
    total_amount: Decimal = Field(max_digits=14, decimal_places=2)
    status: SplitBillStatus = Field(
        default=SplitBillStatus.OPEN, sa_column=named_enum_column(SplitBillStatus, "split_bill_status")
    )

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=tz_aware_column())


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

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=tz_aware_column())
    paid_at: datetime | None = Field(default=None, sa_column=tz_aware_column(nullable=True))
