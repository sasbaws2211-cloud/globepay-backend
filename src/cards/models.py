import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from sqlmodel import Field, SQLModel

from src.common.db_types import named_enum_column, tz_aware_column


class CardStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"  # waiting for the sender's GHS payment
    PROVISIONING = "provisioning"  # paid, Bitnob card creation in flight
    ACTIVE = "active"
    FROZEN = "frozen"
    TERMINATED = "terminated"
    FAILED = "failed"  # GHS payment itself failed - nothing was ever collected
    DELIVERY_FAILED = "delivery_failed"  # GHS payment succeeded but Bitnob card creation didn't - retry or refund
    # Paystack accepted the refund but hasn't paid it out yet; refund.processed
    # moves it to REFUNDED, refund.failed back to DELIVERY_FAILED (see payments/refunds.py).
    REFUND_PENDING = "refund_pending"
    REFUNDED = "refunded"  # DELIVERY_FAILED resolved by refunding the GHS payment instead of retrying


class FundingStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    COMPLETED = "completed"
    FAILED = "failed"  # GHS payment itself failed - nothing was ever collected
    DELIVERY_FAILED = "delivery_failed"  # GHS payment succeeded but the Bitnob top-up didn't - retry or refund
    REFUND_PENDING = "refund_pending"  # see CardStatus.REFUND_PENDING
    REFUNDED = "refunded"


class VirtualCard(SQLModel, table=True):
    """DEMO/PITCH FEATURE - sandbox only, see src/cards/bitnob_cards.py.
    User pays GHS via Paystack (test mode); on confirmation we create a
    Bitnob sandbox lite card (bypasses the full async Card KYC flow,
    which got stuck with kyc_status never leaving "" even with complete
    demographic data submitted) funded with the USD equivalent. No code
    path here can reach real money or a real spendable card."""

    __tablename__ = "virtual_cards"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)

    bitnob_card_id: str | None = Field(default=None)
    bitnob_customer_id: str | None = Field(default=None)

    status: CardStatus = Field(
        default=CardStatus.PENDING_PAYMENT, sa_column=named_enum_column(CardStatus, "virtual_card_status")
    )
    masked_pan: str | None = Field(default=None)
    card_brand: str | None = Field(default=None)
    balance: Decimal = Field(default=Decimal("0.00"), max_digits=10, decimal_places=2)  # USD, last synced from Bitnob
    currency: str = Field(default="USD", max_length=3)

    initial_funding_ghs: Decimal = Field(max_digits=14, decimal_places=2)
    payment_reference: str | None = Field(default=None, index=True)
    failure_reason: str | None = Field(default=None)

    # Persisted (rather than only riding along in Paystack webhook metadata)
    # so a delivery retry can re-attempt Bitnob card creation later without
    # needing the original webhook payload again.
    dial_code: str = Field(default="")
    local_phone_number: str = Field(default="")

    retry_count: int = Field(default=0)
    refund_reference: str | None = Field(default=None)
    refunded_at: datetime | None = Field(default=None, sa_column=tz_aware_column(nullable=True))

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=tz_aware_column())
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=tz_aware_column())


class CardFunding(SQLModel, table=True):
    """A top-up after the card already exists - initial funding happens
    inline as part of card creation instead, since Bitnob's lite-card
    endpoint bundles the two into one call."""

    __tablename__ = "card_fundings"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    card_id: uuid.UUID = Field(foreign_key="virtual_cards.id", index=True)

    amount_ghs: Decimal = Field(max_digits=14, decimal_places=2)
    amount_usd: Decimal = Field(max_digits=10, decimal_places=2)
    status: FundingStatus = Field(
        default=FundingStatus.PENDING_PAYMENT, sa_column=named_enum_column(FundingStatus, "card_funding_status")
    )
    payment_reference: str | None = Field(default=None, index=True)
    failure_reason: str | None = Field(default=None)

    retry_count: int = Field(default=0)
    refund_reference: str | None = Field(default=None)
    refunded_at: datetime | None = Field(default=None, sa_column=tz_aware_column(nullable=True))

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=tz_aware_column())
