import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from src.auth.models import KycTier


class AdminUserSummary(BaseModel):
    id: uuid.UUID
    phone_number: str
    full_name: str
    email: str | None
    is_active: bool
    is_admin: bool
    kyc_tier: KycTier = KycTier.UNVERIFIED
    created_at: datetime


class AdminUserDetail(AdminUserSummary):
    vault_count: int
    total_vault_balance: Decimal


class AdminUserStatusUpdate(BaseModel):
    is_active: bool


class AdminKycTierUpdate(BaseModel):
    kyc_tier: KycTier
    reason: str = Field(min_length=3, max_length=500)  # e.g. "Ghana Card checked in branch" - kept in the audit log


class StuckTransaction(BaseModel):
    kind: str  # "crossborder_transfer" | "card_creation"
    id: uuid.UUID
    user_id: uuid.UUID
    user_phone: str
    amount: Decimal
    status: str
    failure_reason: str | None
    retry_count: int
    created_at: datetime


class AuditLogEntry(BaseModel):
    id: uuid.UUID
    admin_user_id: uuid.UUID
    admin_name: str
    action: str
    target_type: str
    target_id: uuid.UUID
    details: dict | None
    created_at: datetime


class PlatformStats(BaseModel):
    total_users: int
    total_vaults: int
    total_vault_balance: Decimal
    total_fees_collected: Decimal
    stuck_transaction_count: int
