import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field

from src.cards.models import CardStatus, TerminationPayoutStatus


class CardCreate(BaseModel):
    # Min/max load are enforced in service.price_load (they depend on the
    # GHS->USD rate); this only rejects obviously malformed values.
    # initial_funding_ghs is what goes ON the card - Bitnob's fees are added on top.
    initial_funding_ghs: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    sender_email: EmailStr  # for the Paystack checkout - "not-an-email" used to get through
    # Bitnob keys lite-card customers by phone number.
    dial_code: str = Field(pattern=r"^\+\d{1,4}$")  # e.g. "+233"
    local_phone_number: str = Field(min_length=1, max_length=20)  # e.g. "200000001"


class CardCreateResponse(BaseModel):
    authorization_url: str
    reference: str


class CardRead(BaseModel):
    id: uuid.UUID
    status: CardStatus
    masked_pan: str | None
    card_brand: str | None
    balance: Decimal
    currency: str
    failure_reason: str | None
    retry_count: int
    refund_reference: str | None
    refunded_at: datetime | None
    initial_funding_ghs: Decimal | None = None  # what was loaded when the card was created
    fee_ghs: Decimal | None = None  # Bitnob's creation fees, passed on to the user
    # Leftover balance paid to the owner's mobile money after termination.
    termination_refund_usd: Decimal | None = None
    termination_payout_ghs: Decimal | None = None
    termination_payout_status: TerminationPayoutStatus = TerminationPayoutStatus.NOT_STARTED
    # Bitnob's decline rule: 3 strikes close the card; fees taken from the payout.
    decline_strikes: int = 0
    decline_fees_usd: Decimal = Decimal("0")
    last_decline_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CardLimitsRead(BaseModel):
    """Everything the app shows before a card is paid for (see service.get_card_limits)."""

    card_type: str  # always "lite" - the only type this app issues
    can_top_up: bool  # always False for lite cards: funded once, at creation
    min_load_ghs: Decimal
    max_load_ghs: Decimal
    min_load_usd: Decimal
    max_load_usd: Decimal
    max_cards_per_phone: int
    creation_fee_usd: Decimal  # Bitnob's per-card fee - now passed on to the user
    ghs_per_usd: Decimal  # rate for loads and for paying a terminated card's balance back
    fees: dict  # {creation_usd, funding_flat_usd, funding_flat_below_usd, funding_percent}


class CardQuoteRead(BaseModel):
    """Price of a new card before paying: what goes on the card, Bitnob's fees
    (passed on), and the total charged. See service.price_load."""

    amount_ghs: Decimal
    amount_usd: Decimal
    fee_ghs: Decimal
    fee_usd: Decimal
    total_ghs: Decimal


class CardTerminate(BaseModel):
    reason: str


class CardDetailsRequest(BaseModel):
    # Re-entered before the full number/CVV is shown (see cards/secure_details.py).
    password: str = Field(min_length=1, max_length=128)


class CardBillingAddress(BaseModel):
    line1: str | None = None
    line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None


class CardDetailsRead(BaseModel):
    card_number: str
    cvv: str
    expiry_month: str
    expiry_year: str
    name: str | None = None
    card_brand: str | None = None
    billing_address: CardBillingAddress | None = None



class CardTransactionRead(BaseModel):
    """Unified card activity line for the dashboard history view."""

    id: str
    card_id: uuid.UUID
    kind: str  # initial_funding | refund | debit | credit | authorization | decline | reversal | status | other
    direction: str  # credit | debit | neutral
    amount: Decimal | None  # primary display amount (USD when from Bitnob, GHS for the initial load)
    currency: str
    amount_ghs: Decimal | None = None
    amount_usd: Decimal | None = None
    status: str
    description: str
    merchant_name: str | None = None
    reference: str | None = None
    source: str  # local | bitnob
    created_at: datetime
