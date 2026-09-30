import uuid
from datetime import datetime
from decimal import Decimal

from typing import Any

from pydantic import BaseModel, EmailStr, Field, field_validator

from src.crossborder.models import CrossBorderStatus


class CrossBorderInitiate(BaseModel):
    source_amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)  # GHS
    destination_country: str = Field(pattern=r"^[A-Za-z]{2}$")  # ISO code, e.g. "KE"
    destination_currency: str = Field(pattern=r"^[A-Za-z]{3}$")  # e.g. "KES"
    # How the money is delivered - one of the corridor's destination types
    # from Bitnob (mobile_money, bank, ach, wire, sepa_eur, domestic_gbp, swift...).
    destination_type: str = Field(min_length=1, max_length=40)
    # The fields Bitnob requires for that destination type (see
    # GET /crossborder/corridors/{country}); validated in corridors.build_beneficiary
    # against Bitnob's live schema, including nested `beneficiary` and `sender`.
    beneficiary: dict[str, Any]
    sender_email: EmailStr  # for the Paystack checkout
    # Remember the sender block (address, date/country of birth) for next time.
    save_sender_profile: bool = False

    @field_validator("destination_country", "destination_currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        # Bitnob looks corridors up case-sensitively ("KE/ngn" was not found).
        return value.upper()


class SenderProfile(BaseModel):
    sender: dict[str, Any] | None = None  # as last used in a transfer's `sender` block


class CrossBorderInitiateResponse(BaseModel):
    authorization_url: str
    reference: str


class CrossBorderRead(BaseModel):
    id: uuid.UUID
    source_amount: Decimal
    destination_country: str
    destination_currency: str
    destination_amount: Decimal | None
    exchange_rate_used: Decimal | None
    status: CrossBorderStatus
    bitnob_status: str | None
    failure_reason: str | None
    retry_count: int
    refund_reference: str | None
    refunded_at: datetime | None
    created_at: datetime
    completed_at: datetime | None
    # Filled by routes: how and to whom it was sent (account masked).
    destination_type: str | None = None
    beneficiary_name: str | None = None
    beneficiary_bank: str | None = None
    beneficiary_account: str | None = None
