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
