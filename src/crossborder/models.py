import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

from src.common.db_types import named_enum_column, tz_aware_column


class CrossBorderStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"  # waiting for sender's GHS payment
    PROCESSING = "processing"  # paid, Bitnob sandbox payout in flight
    COMPLETED = "completed"
    FAILED = "failed"  # GHS payment itself failed - nothing was ever collected
    DELIVERY_FAILED = "delivery_failed"  # GHS payment succeeded but the Bitnob payout didn't - retry or refund
    # Paystack accepted the refund but hasn't paid it out yet; refund.processed
    # moves it to REFUNDED, refund.failed back to DELIVERY_FAILED (see payments/refunds.py).
    REFUND_PENDING = "refund_pending"
    REFUNDED = "refunded"  # DELIVERY_FAILED resolved by refunding the GHS payment instead of retrying


class CrossBorderTransfer(SQLModel, table=True):
    """DEMO/PITCH FEATURE - sandbox only, see src/crossborder/bitnob.py.
    Sender pays GHS via Paystack (test mode); on confirmation we get a
    Bitnob sandbox quote (USDC -> destination currency) and finalize it
    against the recipient's mobile money details. No code path here can
    reach real money."""

    __tablename__ = "crossborder_transfers"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    sender_id: uuid.UUID = Field(foreign_key="users.id", index=True)

    source_amount: Decimal = Field(max_digits=14, decimal_places=2)  # GHS
    destination_country: str = Field(max_length=2)  # ISO country code
    destination_currency: str = Field(max_length=3)
    destination_amount: Decimal | None = Field(default=None, max_digits=14, decimal_places=2)
    exchange_rate_used: Decimal | None = Field(default=None, max_digits=18, decimal_places=8)

    from_asset: str = Field(default="USDC")  # matches the funded sandbox test balance
    quote_id: str | None = Field(default=None)  # Bitnob's quote_id, e.g. "QT2_21052260" - used in POST paths
    bitnob_id: str | None = Field(default=None)  # Bitnob's internal UUID - GET /api/payouts/{id} needs this, not quote_id

    # Shaped to match Bitnob's `beneficiary` payload directly ({destination_type,
    # country, account_name, account_number, network}) - kept as JSON since
    # this is a demo feature mirroring a third-party API's schema, not a
    # core domain model.
    beneficiary_details: dict = Field(sa_column=Column(JSON, nullable=False))

    status: CrossBorderStatus = Field(
        default=CrossBorderStatus.PENDING_PAYMENT,
        sa_column=named_enum_column(CrossBorderStatus, "crossborder_status"),
    )
    payment_reference: str | None = Field(default=None, index=True)
    bitnob_status: str | None = Field(default=None)
    failure_reason: str | None = Field(default=None)

    retry_count: int = Field(default=0)  # number of manual retry attempts while DELIVERY_FAILED
    refund_reference: str | None = Field(default=None)
    refunded_at: datetime | None = Field(default=None, sa_column=tz_aware_column(nullable=True))

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=tz_aware_column())
    completed_at: datetime | None = Field(default=None, sa_column=tz_aware_column(nullable=True))
