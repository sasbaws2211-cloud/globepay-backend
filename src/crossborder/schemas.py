import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, field_validator

from src.crossborder.models import CrossBorderStatus


class BeneficiaryDetails(BaseModel):
    destination_type: str = "mobile_money"  # mobile_money / bank
    account_name: str
    account_number: str  # phone number for mobile_money
    network: str  # e.g. MPESA, MTN, AIRTEL - confirmed field name for mobile_money

    @field_validator("network")
    @classmethod
    def _bitnob_network_code(cls, value: str) -> str:
        # Bitnob only accepts bare uppercase codes - confirmed live: "M-Pesa"
        # fails delivery with "network has an invalid value" *after* the
        # sender has paid, so normalize the familiar spellings up front.
        return "".join(ch for ch in value if ch.isalnum()).upper()


class CrossBorderInitiate(BaseModel):
    source_amount: Decimal  # GHS
    destination_country: str  # ISO code, e.g. "KE"
    destination_currency: str  # e.g. "KES"
    beneficiary: BeneficiaryDetails
    sender_email: str  # for the Paystack checkout


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
