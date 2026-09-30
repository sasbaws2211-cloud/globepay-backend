import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from src.vaults.models import ContributionStatus, RecurringStatus, VaultFrequency, VaultStatus, WithdrawalStatus


class VaultCreate(BaseModel):
    # Confirmed live 2026-09-29: none of these were validated - a GHS 0 or
    # negative target, a negative contribution, a 2020 lock date and a blank
    # name were all accepted.
    name: str = Field(min_length=1, max_length=100)
    target_amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    contribution_amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    frequency: VaultFrequency
    lock_until: date

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name can't be blank")
        return value

    @field_validator("lock_until")
    @classmethod
    def _lock_not_in_past(cls, value: date) -> date:
        if value < date.today():
            raise ValueError("Lock date can't be in the past")
        return value

    @model_validator(mode="after")
    def _contribution_within_target(self):
        if self.contribution_amount > self.target_amount:
            raise ValueError("Each contribution can't be more than the target")
        return self


class VaultRead(BaseModel):
    id: uuid.UUID
    name: str
    target_amount: Decimal
    contribution_amount: Decimal
    frequency: VaultFrequency
    balance: Decimal
    lock_until: date
    status: VaultStatus
    recurring_status: RecurringStatus
    next_charge_date: date | None
    recurring_consecutive_failures: int
    recurring_last_failure_reason: str | None
    created_at: datetime
    # Latest withdrawal's payout state (pending / completed / failed), so the
    # app can say whether the money has actually arrived - the vault itself
    # flips to "withdrawn" as soon as the payout is requested.
    last_withdrawal_status: WithdrawalStatus | None = None
    # What withdrawing the whole balance now would cost and pay out, so the
    # app shows it before the owner confirms (see service.withdrawal_quote).
    withdrawal_fee: Decimal | None = None
    withdrawal_net: Decimal | None = None


class ContributionInitiate(BaseModel):
    # A GHS 0 / negative amount used to reach Paystack ("Invalid Amount Sent")
    # and come back as a 500, leaving a pending contribution row behind.
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    email: EmailStr


class ContributionInitiateResponse(BaseModel):
    authorization_url: str
    reference: str


class ContributionRead(BaseModel):
    id: uuid.UUID
    amount: Decimal
    status: ContributionStatus
    paid_at: datetime | None
    created_at: datetime


class WithdrawalRequest(BaseModel):
    momo_number: str
    momo_network_bank_code: str
    account_name: str


class WithdrawalRead(BaseModel):
    id: uuid.UUID
    gross_amount: Decimal
    platform_fee: Decimal
    net_amount: Decimal
    status: WithdrawalStatus
    created_at: datetime
