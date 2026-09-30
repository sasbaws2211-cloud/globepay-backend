import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from src.splitbill.models import (
    SharePayoutStatus,
    ShareStatus,
    SplitBillStatus,
    SplitWithdrawalStatus,
    WithdrawalDestination,
)


class SplitBillCreate(BaseModel):
    # Confirmed live 2026-09-29: a GHS 0 / negative total and a blank title were accepted.
    title: str = Field(min_length=1, max_length=120)
    total_amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    # Everyone who owes a share, not including the organizer - the organizer
    # is part of the split automatically (see service.create_split_bill).
    participant_phone_numbers: list[str] = Field(min_length=1, max_length=50)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Title can't be blank")
        return value


class ShareRead(BaseModel):
    id: uuid.UUID
    split_bill_id: uuid.UUID
    user_id: uuid.UUID
    gross_amount: Decimal
    status: ShareStatus
    payout_status: SharePayoutStatus
    created_at: datetime
    paid_at: datetime | None
    participant_name: str | None = None  # filled by routes._to_read
    is_organizer: bool = False  # the organizer's own portion - nothing to collect or pay out


class SplitWithdrawalRead(BaseModel):
    id: uuid.UUID
    destination: WithdrawalDestination
    vault_id: uuid.UUID | None = None
    vault_name: str | None = None  # filled by routes._to_read
    amount: Decimal  # what the organizer receives
    fee: Decimal = Decimal("0.00")  # platform fee taken - 0 for a vault
    status: SplitWithdrawalStatus
    failure_reason: str | None = None
    attempts: int
    updated_at: datetime


class SplitBillRead(BaseModel):
    id: uuid.UUID
    organizer_id: uuid.UUID
    title: str
    total_amount: Decimal
    status: SplitBillStatus
    created_at: datetime
    shares: list[ShareRead]
    # True for bills that hold paid shares until the organizer withdraws;
    # False for older bills that paid each share out as it came in.
    collects_funds: bool = False
    # Collecting bills, organizer only (None for participants). What was paid
    # (= what a vault withdrawal gets, no fee), what mobile money gets after
    # the platform fee, and the withdrawal once one has been started.
    collected_gross_amount: Decimal | None = None
    collected_amount: Decimal | None = None
    withdrawal: SplitWithdrawalRead | None = None


class SplitWithdrawRequest(BaseModel):
    destination: WithdrawalDestination
    vault_id: uuid.UUID | None = None  # required for destination "vault"

    @model_validator(mode="after")
    def _vault_for_vault(self) -> "SplitWithdrawRequest":
        if self.destination == WithdrawalDestination.VAULT and self.vault_id is None:
            raise ValueError("Choose a vault")
        return self


class SharePayInitiate(BaseModel):
    email: EmailStr


class SharePayInitiateResponse(BaseModel):
    authorization_url: str
    reference: str
