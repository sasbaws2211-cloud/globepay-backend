import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import JSON, Column, UniqueConstraint
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


class TerminationPayoutStatus(StrEnum):
    """Paying a terminated card's leftover balance back to the cardholder."""

    NOT_STARTED = "not_started"  # card not terminated yet
    NOT_NEEDED = "not_needed"  # nothing (or less than MIN_PAYOUT_GHS) was left
    PENDING = "pending"  # Paystack transfer requested; settles by webhook or reconcile sweep
    COMPLETED = "completed"
    FAILED = "failed"  # couldn't be started or bounced - the owner can retry


class VirtualCard(SQLModel, table=True):
    """DEMO/PITCH FEATURE - sandbox only, see src/cards/bitnob_cards.py.
    User pays GHS via Paystack (test mode); on confirmation we create a
    Bitnob sandbox lite card funded with the USD equivalent. Lite is the only
    card type GlobePay issues: no identity check, loaded once at creation
    (max $250), never topped up or withdrawn from. No code path here can
    reach real money or a real spendable card."""

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

    initial_funding_ghs: Decimal = Field(max_digits=14, decimal_places=2)  # what goes onto the card
    # Bitnob's fees for creating the card, passed on to the user on top of the load.
    fee_ghs: Decimal = Field(default=Decimal("0.00"), max_digits=14, decimal_places=2)
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

    # What was left on the card when it was terminated. Bitnob returns it to the
    # company wallet, not the cardholder (lite cards can't be withdrawn from), so
    # GlobePay pays the GHS equivalent to their mobile money - see
    # cards/termination_payout.py.
    termination_refund_usd: Decimal | None = Field(default=None, max_digits=10, decimal_places=2)
    termination_payout_ghs: Decimal | None = Field(default=None, max_digits=14, decimal_places=2)
    termination_payout_status: TerminationPayoutStatus = Field(
        default=TerminationPayoutStatus.NOT_STARTED,
        sa_column=named_enum_column(TerminationPayoutStatus, "card_termination_payout_status"),
    )
    termination_payout_reference: str | None = Field(default=None, index=True)

    # Bitnob's decline rule: declines for low balance, attempts on a frozen card
    # and approved-but-never-completed payments are "violations". 1st free, 2nd
    # and 3rd cost $0.75 each (charged to the company wallet - passed on, taken
    # from the termination payout), and the 3rd terminates the card for good.
    # See cards/decline_rule.py.
    decline_strikes: int = Field(default=0)
    decline_fees_usd: Decimal = Field(default=Decimal("0.00"), max_digits=10, decimal_places=2)
    last_decline_at: datetime | None = Field(default=None, sa_column=tz_aware_column(nullable=True))

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=tz_aware_column())
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=tz_aware_column())

    @property
    def charged_ghs(self) -> Decimal:
        """What the user paid via Paystack: the load plus the passed-on fee."""
        return self.initial_funding_ghs + (self.fee_ghs or Decimal("0"))


class CardEvent(SQLModel, table=True):
    """One Bitnob card webhook, stored as received.

    Before this, GlobePay only learned a card had been used when someone
    opened the card - spending, declines and terminations went unrecorded.
    `event_id` is Bitnob's own id (constant across its retries), so a
    redelivered webhook is recognised and not processed twice."""

    __tablename__ = "card_events"
    __table_args__ = (UniqueConstraint("event_id", name="uq_card_events_event_id"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    event_id: str = Field(index=True)
    event_type: str = Field(index=True)  # e.g. "virtualcard.transaction.debit"
    card_id: uuid.UUID | None = Field(default=None, foreign_key="virtual_cards.id", index=True)  # None if not ours
    bitnob_card_id: str | None = Field(default=None, index=True)
    amount_usd: Decimal | None = Field(default=None, max_digits=14, decimal_places=2)
    reference: str | None = Field(default=None)
    status: str | None = Field(default=None)
    reason: str | None = Field(default=None)  # decline / failure reason, as Bitnob gave it
    merchant: str | None = Field(default=None)
    payload: dict = Field(sa_column=Column(JSON, nullable=False))  # the raw event, for support/audit
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=tz_aware_column())
