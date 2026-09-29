import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from src.splitbill.models import SharePayoutStatus, ShareStatus, SplitBillStatus


class SplitBillCreate(BaseModel):
    title: str
    total_amount: Decimal
    participant_phone_numbers: list[str]  # organizer is excluded automatically


class ShareRead(BaseModel):
    id: uuid.UUID
    split_bill_id: uuid.UUID
    user_id: uuid.UUID
    gross_amount: Decimal
    status: ShareStatus
    payout_status: SharePayoutStatus
    created_at: datetime
    paid_at: datetime | None


class SplitBillRead(BaseModel):
    id: uuid.UUID
    organizer_id: uuid.UUID
    title: str
    total_amount: Decimal
    status: SplitBillStatus
    created_at: datetime
    shares: list[ShareRead]


class SharePayInitiate(BaseModel):
    email: str


class SharePayInitiateResponse(BaseModel):
    authorization_url: str
    reference: str
