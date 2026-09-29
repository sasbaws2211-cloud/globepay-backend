import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from src.cards.models import CardStatus, FundingStatus


class CardCreate(BaseModel):
    initial_funding_ghs: Decimal
    sender_email: str  # for the Paystack checkout
    dial_code: str  # e.g. "+233" - Bitnob's lite-card customer object wants these
    local_phone_number: str  # without the dial code, e.g. "200000001"


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
    creation_fee_usd: Decimal  # Bitnob's fee, charged to the platform wallet, not the card


class CardFundingCreate(BaseModel):
    amount_ghs: Decimal
    sender_email: str


class CardFundingResponse(BaseModel):
    authorization_url: str
    reference: str


class CardFundingRead(BaseModel):
    id: uuid.UUID
    amount_ghs: Decimal
    amount_usd: Decimal
    status: FundingStatus
    failure_reason: str | None
    retry_count: int
    refund_reference: str | None
    refunded_at: datetime | None
    created_at: datetime


class CardTerminate(BaseModel):
    reason: str



class CardTransactionRead(BaseModel):
    """Unified card activity line for the dashboard history view."""

    id: str
    card_id: uuid.UUID
    kind: str  # funding | initial_funding | refund | debit | credit | authorization | decline | reversal | status | other
    direction: str  # credit | debit | neutral
    amount: Decimal | None  # primary display amount (USD when from Bitnob, GHS for local funding)
    currency: str
    amount_ghs: Decimal | None = None
    amount_usd: Decimal | None = None
    status: str
    description: str
    merchant_name: str | None = None
    reference: str | None = None
    source: str  # local | bitnob
    created_at: datetime
