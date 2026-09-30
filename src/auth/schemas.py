import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, EmailStr

from src.auth.models import KycTier, ReferralRewardStatus


class UserCreate(BaseModel):
    phone_number: str
    full_name: str
    email: EmailStr | None = None
    password: str
    referral_code: str | None = None


class UserRead(BaseModel):
    id: uuid.UUID
    phone_number: str
    full_name: str
    email: str | None
    is_phone_verified: bool
    kyc_tier: KycTier = KycTier.UNVERIFIED  # sets the transaction limits - see GET /auth/me/limits
    referral_code: str
    referral_reward_status: ReferralRewardStatus
    default_momo_number: str | None
    default_momo_bank_code: str | None
    default_account_name: str | None = None  # the saved payout's account name, to prefill withdrawals
    round_up_vault_id: uuid.UUID | None
    round_up_denomination: Decimal
    # A reusable card from an earlier card payment - needed for auto-contribute.
    has_saved_card: bool = False
    paystack_card_last4: str | None = None


class TransactionLimitsRead(BaseModel):
    """The user's verification tier, its limits, and what's used so far
    (confirmed payments plus checkouts started in the last 30 minutes)."""

    enforced: bool = False  # settings.ENFORCE_TRANSACTION_LIMITS - when False nothing is refused
    kyc_tier: KycTier
    daily_limit: Decimal | None  # None = no daily limit for this tier
    daily_used: Decimal
    daily_remaining: Decimal | None
    monthly_limit: Decimal | None  # None = no monthly limit for this tier
    monthly_used: Decimal
    monthly_remaining: Decimal | None


class ReferredUserRead(BaseModel):
    full_name: str
    referral_reward_status: ReferralRewardStatus
    created_at: datetime


class PayoutDestinationSet(BaseModel):
    momo_number: str
    momo_bank_code: str  # MTN / ATL / VOD
    account_name: str


class RoundUpSettingsSet(BaseModel):
    vault_id: uuid.UUID | None  # null disables round-up
    denomination: Decimal = Decimal("5.00")


class UserLogin(BaseModel):
    phone_number: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class PasswordResetRequest(BaseModel):
    phone_number: str


class PasswordResetConfirm(BaseModel):
    phone_number: str
    code: str
    new_password: str


class PhoneVerificationConfirm(BaseModel):
    code: str


class AccountClosureRequest(BaseModel):
    password: str
