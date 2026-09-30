import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field

from src.wallet.models import TransferStatus


class TransferInitiate(BaseModel):
    recipient_phone_number: str
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    note: str | None = None
    sender_email: EmailStr


class TransferInitiateResponse(BaseModel):
    authorization_url: str
    reference: str
    transfer_id: uuid.UUID | None = None  # absent on idempotent replays recorded before it was added


class TransferRead(BaseModel):
    id: uuid.UUID
    sender_id: uuid.UUID
    recipient_id: uuid.UUID
    gross_amount: Decimal
    platform_fee: Decimal
    net_amount: Decimal
    note: str | None
    roundup_amount: Decimal
    status: TransferStatus
    created_at: datetime
    completed_at: datetime | None
    # Viewer-relative (filled by service.to_read): which side of the transfer
    # the requesting user is on, and who the other party is.
    direction: str | None = None  # "sent" | "received"
    counterparty_name: str | None = None
    counterparty_phone: str | None = None  # masked, e.g. "+233 20 *** 0002"
    # Only for the sender of an unpaid transfer: reopen its Paystack checkout.
    pay_url: str | None = None


class TransferQuoteRequest(BaseModel):
    recipient_phone_number: str
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)


class TransferQuote(BaseModel):
    """What the sender sees before paying - nothing is created or charged."""

    recipient_name: str  # first name + last initial, to confirm the right person
    recipient_phone: str  # masked
    amount: Decimal  # what the sender is sending
    platform_fee: Decimal  # deducted from what the recipient gets
    recipient_gets: Decimal
    roundup_amount: Decimal  # swept into the sender's round-up vault, if enabled
    total_charge: Decimal  # what Paystack will charge the sender


class WalletSummary(BaseModel):
    received_total: Decimal
    sent_total: Decimal
    fee_total: Decimal
    roundup_total: Decimal
    net_flow: Decimal


class TransferClaim(BaseModel):
    momo_number: str
    momo_bank_code: str
    account_name: str
    save_as_default: bool = True
