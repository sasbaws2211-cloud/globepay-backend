import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.models import KycTier, Otp, OtpPurpose, ReferralRewardStatus, User
from src.auth.schemas import UserCreate
from src.auth.utils import hash_password
from src.common.phone import normalize_gh_phone, phone_lookup_variants
from src.common.sms import send_sms
from src.vaults.models import RecurringStatus, Vault

OTP_TTL = timedelta(minutes=10)
MAX_OTP_ATTEMPTS = 5


async def get_user_by_phone(session: AsyncSession, phone_number: str) -> User | None:
    """Matches any spelling of the number ("0244...", "+233244...", ...) -
    see src/common/phone.py. Used by login, password reset, registration's
    duplicate check, wallet transfers and split bills."""
    result = await session.exec(select(User).where(User.phone_number.in_(phone_lookup_variants(phone_number))))
    return result.first()


async def get_user_by_referral_code(session: AsyncSession, referral_code: str) -> User | None:
    result = await session.exec(select(User).where(User.referral_code == referral_code))
    return result.first()


def _generate_referral_code() -> str:
    # Not checked for collisions before insert - same risk tolerance as
    # every other reference generator in this codebase (e.g. payment
    # references), and negligible at this app's scale.
    return secrets.token_hex(4).upper()


async def create_user(session: AsyncSession, user_data: UserCreate) -> User:
    referred_by: User | None = None
    if user_data.referral_code:
        referred_by = await get_user_by_referral_code(session, user_data.referral_code)
        if referred_by is None:
            raise HTTPException(status_code=400, detail="Invalid referral code")

    user = User(
        phone_number=normalize_gh_phone(user_data.phone_number),
        full_name=user_data.full_name,
        email=user_data.email,
        hashed_password=hash_password(user_data.password),
        referral_code=_generate_referral_code(),
        referred_by_id=referred_by.id if referred_by else None,
        referral_reward_status=ReferralRewardStatus.PENDING if referred_by else ReferralRewardStatus.NONE,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def list_my_referrals(session: AsyncSession, referrer_id: uuid.UUID) -> list[User]:
    result = await session.exec(
        select(User).where(User.referred_by_id == referrer_id).order_by(User.created_at.desc())
    )
    return list(result.all())


def _hash_otp(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


async def _issue_otp(session: AsyncSession, user: User, purpose: OtpPurpose) -> str:
    """Invalidates any outstanding code for this user+purpose before issuing
    a new one, so an old code doesn't stay valid alongside the new one
    until it naturally expires. Returns the plaintext code to send by SMS -
    only the hash is ever persisted."""
    existing = await session.exec(
        select(Otp).where(Otp.user_id == user.id, Otp.purpose == purpose, Otp.used_at.is_(None))
    )
    now = datetime.now(timezone.utc)
    for otp in existing.all():
        otp.used_at = now
        session.add(otp)

    code = f"{secrets.randbelow(1_000_000):06d}"
    session.add(Otp(user_id=user.id, purpose=purpose, code_hash=_hash_otp(code), expires_at=now + OTP_TTL))
    await session.commit()
    return code


async def _consume_otp(session: AsyncSession, user: User, purpose: OtpPurpose, code: str) -> None:
    """Raises 400 on any failure (no code, wrong code, expired, too many
    attempts) without distinguishing which - marks the matching OTP used
    on success. Caller applies whatever the OTP was for."""
    invalid = HTTPException(status_code=400, detail="Invalid or expired code")

    result = await session.exec(
        select(Otp)
        .where(Otp.user_id == user.id, Otp.purpose == purpose, Otp.used_at.is_(None))
        .order_by(Otp.created_at.desc())
    )
    otp = result.first()
    if otp is None or otp.expires_at < datetime.now(timezone.utc):
        raise invalid
    if otp.attempts >= MAX_OTP_ATTEMPTS:
        raise HTTPException(status_code=400, detail="Too many incorrect attempts - request a new code")

    if not hmac.compare_digest(otp.code_hash, _hash_otp(code)):
        otp.attempts += 1
        session.add(otp)
        await session.commit()
        raise invalid

    otp.used_at = datetime.now(timezone.utc)
    session.add(otp)


async def request_password_reset(session: AsyncSession, phone_number: str) -> None:
    """Always returns (no error either way) regardless of whether the phone
    number is registered - the route must never let an attacker use it to
    check which numbers have accounts."""
    user = await get_user_by_phone(session, phone_number)
    if user is None:
        return

    code = await _issue_otp(session, user, OtpPurpose.PASSWORD_RESET)
    await send_sms(user.phone_number, f"Your password reset code is {code}. It expires in 10 minutes.")


async def confirm_password_reset(session: AsyncSession, phone_number: str, code: str, new_password: str) -> User:
    user = await get_user_by_phone(session, phone_number)
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    await _consume_otp(session, user, OtpPurpose.PASSWORD_RESET, code)

    user.hashed_password = hash_password(new_password)
    # Bumping this is what actually revokes every token issued before this
    # reset (see get_current_user) - without it, a reset done because an
    # account was compromised wouldn't lock out whoever already had a token.
    user.token_version += 1
    user.updated_at = datetime.now(timezone.utc)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def request_phone_verification(session: AsyncSession, user: User) -> None:
    if user.is_phone_verified:
        raise HTTPException(status_code=400, detail="Phone number is already verified")
    code = await _issue_otp(session, user, OtpPurpose.PHONE_VERIFICATION)
    await send_sms(user.phone_number, f"Your phone verification code is {code}. It expires in 10 minutes.")


async def confirm_phone_verification(session: AsyncSession, user: User, code: str) -> User:
    if user.is_phone_verified:
        raise HTTPException(status_code=400, detail="Phone number is already verified")

    await _consume_otp(session, user, OtpPurpose.PHONE_VERIFICATION, code)

    user.is_phone_verified = True
    # Verifying the phone raises the transaction limits (common/kyc_limits.py);
    # never lowers an ID-verified user.
    if user.kyc_tier == KycTier.UNVERIFIED:
        user.kyc_tier = KycTier.PHONE_VERIFIED
    user.updated_at = datetime.now(timezone.utc)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def close_account(session: AsyncSession, user: User) -> None:
    """Deactivate + clear what's safe to clear, not a hard delete: this app
    is not a real regulated fintech, but modeling one honestly means a
    closed account's identity should stay linked to its transaction
    history for whatever AML retention period would apply, not be erased
    on request. phone_number and every
    contribution/transfer/audit row are left untouched. What does get
    cleared is data with no such retention reason: contact details, the
    saved card, payout destination, and the submitted Ghana Card number.

    Blocks when a vault balance would become unreachable. """
    vault_result = await session.exec(select(Vault).where(Vault.owner_id == user.id))
    vaults = list(vault_result.all())
    if any(v.balance > 0 for v in vaults):
        raise HTTPException(status_code=400, detail="Withdraw all vault balances before closing your account")

    for vault in vaults:
        if vault.recurring_status != RecurringStatus.INACTIVE:
            vault.recurring_status = RecurringStatus.INACTIVE
            vault.next_charge_date = None
            session.add(vault)

    user.full_name = "Closed Account"
    user.email = None
    user.paystack_authorization_code = None
    user.paystack_card_last4 = None
    user.default_momo_number = None
    user.default_momo_bank_code = None
    user.default_account_name = None
    user.round_up_vault_id = None
    user.is_active = False
    user.closed_at = datetime.now(timezone.utc)
    user.updated_at = datetime.now(timezone.utc)
    session.add(user)
    await session.commit()
