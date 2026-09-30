import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from sqlmodel import Field, SQLModel

from src.common.db_types import tz_aware_column


class TransferStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"  # waiting for sender to pay in
    AWAITING_RECIPIENT_PAYOUT_INFO = "awaiting_recipient_payout_info"
    # Paystack accepted the payout request but hasn't delivered it yet (it may
    # be queued, or held at "otp" if Transfer OTP is enabled on the account).
    # Only the transfer.success webhook moves this to COMPLETED.
    PAYOUT_PENDING = "payout_pending"
    COMPLETED = "completed"
    FAILED = "failed"


class WalletTransfer(SQLModel, table=True):
    """A 'wallet-to-wallet' transfer between two users. No stored balance is
    ever involved: paying in and paying out are two independent, real
    Paystack transactions chained together by this record. The platform
    never holds the money as its own liability at any point."""

    __tablename__ = "wallet_transfers"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    sender_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    recipient_id: uuid.UUID = Field(foreign_key="users.id", index=True)

    gross_amount: Decimal = Field(max_digits=14, decimal_places=2)
    platform_fee: Decimal = Field(max_digits=14, decimal_places=2)
    net_amount: Decimal = Field(max_digits=14, decimal_places=2)

    note: str | None = Field(default=None, max_length=200)

    # Round-up auto-save: if the sender has a round-up vault configured, this
    # is charged on top of gross_amount in the same Paystack transaction and
    # swept into their vault once payment is confirmed.
    roundup_amount: Decimal = Field(default=Decimal("0.00"), max_digits=10, decimal_places=2)

    status: TransferStatus = Field(default=TransferStatus.PENDING_PAYMENT)
    payment_reference: str | None = Field(default=None, index=True)
    # Paystack checkout for this transfer, kept so an unpaid transfer can be
    # paid later (e.g. the sender closed the popup) instead of being stuck.
    authorization_url: str | None = Field(default=None)
    payout_reference: str | None = Field(default=None)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=tz_aware_column())
    completed_at: datetime | None = Field(default=None, sa_column=tz_aware_column(nullable=True))
