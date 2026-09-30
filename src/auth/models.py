import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import JSON, Column, ForeignKey
from sqlalchemy import Uuid as SAUuid
from sqlmodel import Field, SQLModel

from src.common.db_types import named_enum_column, tz_aware_column


class ReferralRewardStatus(StrEnum):
    NONE = "none"  # not referred by anyone
    PENDING = "pending"  # referred, hasn't yet made a qualifying first deposit
    REWARDED = "rewarded"  # both sides already credited - see src/vaults/service.py


class KycTier(StrEnum):
    """Verification level - sets the transaction limits (common/kyc_limits.py)."""

    UNVERIFIED = "unverified"
    PHONE_VERIFIED = "phone_verified"  # confirmed an SMS code to their number
    ID_VERIFIED = "id_verified"  # identity checked (set by an admin)


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    phone_number: str = Field(unique=True, index=True, max_length=20)
    full_name: str
    email: str | None = Field(default=None, unique=True, index=True)
    hashed_password: str

    is_active: bool = Field(default=True)
    is_phone_verified: bool = Field(default=False)
    kyc_tier: KycTier = Field(default=KycTier.UNVERIFIED, sa_column=named_enum_column(KycTier, "kyc_tier"))

    # Embedded in every access token issued at login (see
    # create_access_token/get_current_user) so a password reset actually
    # invalidates every token issued before it, not just future logins - a
    # JWT with only {sub, exp} has no way to do that, and a reset that
    # doesn't revoke already-stolen tokens defeats its own purpose as an
    # incident-response tool. A monotonic counter, not a timestamp - a
    # wall-clock comparison looked right until a test proved two resets (or
    # a login immediately followed by a reset) can land in the same
    # timestamp tick, at which point the "old" token wrongly still passes.
    # Bumped only on a real password change (confirm_password_reset).
    token_version: int = Field(default=1)
    # No self-serve path to True anywhere in the app - only ever set directly
    # against the database by whoever operates the platform. See
    # scripts/promote_admin.py.
    is_admin: bool = Field(default=False)

    # Self-service account closure (src/auth/service.py close_account) -
    # deliberately a separate field from is_active rather than reusing it,
    # so an admin suspension and a user's own closure can never be confused
    # with each other (an admin "reactivating" is_active on a self-closed
    # account should not silently let them log back in). One-way in this
    # version - no reopen endpoint exists. Closing does NOT scramble
    # phone_number or any transaction/ledger row -
    # this app is not a real regulated entity, but the honest modeling of
    # one would need to retain identity-linked financial records for an
    # AML retention period, not erase them on request, so this doesn't
    # pretend otherwise. See close_account for exactly what does get cleared.
    closed_at: datetime | None = Field(default=None, sa_column=tz_aware_column(nullable=True))

    # Sender details bank/SWIFT payouts require (address, date and country of
    # birth, ...), exactly as last sent in a cross-border transfer's `sender`
    # block - saved on request so the form can prefill them next time.
    sender_profile: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))


    # Referrals - see src/vaults/service.py for the reward trigger (a
    # referred user's first confirmed vault contribution). No defense here
    # against a person referring a second account of their own to farm the
    # bonus - would need real identity linking this app doesn't have, so
    # this is a known, accepted limitation rather than a solved one.
    referral_code: str = Field(unique=True, index=True)
    referred_by_id: uuid.UUID | None = Field(default=None, foreign_key="users.id")
    referral_reward_status: ReferralRewardStatus = Field(
        default=ReferralRewardStatus.NONE,
        sa_column=named_enum_column(ReferralRewardStatus, "referral_reward_status"),
    )

    # Saved payout destination - lets money sent to this user land
    # automatically instead of requiring a manual claim every time.
    # Still a real Paystack transfer per payout, never a stored balance.
    default_momo_number: str | None = Field(default=None)
    default_momo_bank_code: str | None = Field(default=None)  # MTN / ATL / VOD
    default_account_name: str | None = Field(default=None)

    # Round-up auto-save: when set, wallet transfers are rounded up to the
    # nearest `round_up_denomination` and the difference is swept into this
    # vault as a real, immediately-paid contribution.
    #
    # users <-> vaults is otherwise a one-way FK (vaults.owner_id -> users.id);
    # this column points the other way and creates a real circular table
    # dependency, which breaks SQLAlchemy's create/drop ordering unless the
    # constraint is added via ALTER TABLE (use_alter) instead of inline.
    round_up_vault_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            SAUuid,
            ForeignKey("vaults.id", use_alter=True, name="fk_users_round_up_vault_id"),
            nullable=True,
        ),
    )
    round_up_denomination: Decimal = Field(default=Decimal("5.00"), max_digits=6, decimal_places=2)

    # Saved card, captured from the `authorization` object on the verify
    # response of any successful vault contribution (see
    # src/vaults/service.py confirm_contribution) whose channel is "card"
    # and which Paystack marked reusable - mobile money authorizations
    # aren't captured here since Paystack doesn't document the same
    # reusable-charge guarantee for that channel. Powers recurring vault
    # contributions (src/scheduler.py) via paystack.charge_authorization.
    paystack_authorization_code: str | None = Field(default=None)
    paystack_card_last4: str | None = Field(default=None)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=tz_aware_column())
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=tz_aware_column())

    @property
    def has_saved_card(self) -> bool:
        """Whether auto-contribute can be switched on - without exposing the token."""
        return bool(self.paystack_authorization_code)


class OtpPurpose(StrEnum):
    PASSWORD_RESET = "password_reset"
    PHONE_VERIFICATION = "phone_verification"


class Otp(SQLModel, table=True):
    """A one-time SMS code, shared by every OTP-based flow (password reset,
    phone verification). code_hash is a plain SHA-256 digest, not bcrypt -
    a 6-digit code has far less entropy than a real password, and bcrypt's
    deliberate slowness buys nothing here since the code is single-use and
    expires in minutes anyway."""

    __tablename__ = "otps"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    purpose: OtpPurpose = Field(sa_column=named_enum_column(OtpPurpose, "otp_purpose"))

    code_hash: str
    attempts: int = Field(default=0)

    used_at: datetime | None = Field(default=None, sa_column=tz_aware_column(nullable=True))
    expires_at: datetime = Field(sa_column=tz_aware_column())
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=tz_aware_column())
