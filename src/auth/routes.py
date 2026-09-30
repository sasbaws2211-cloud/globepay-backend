from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth import service
from src.auth.dependencies import get_current_user
from src.auth.models import User
from src.auth.schemas import (
    AccountClosureRequest,
    PasswordResetConfirm,
    PasswordResetRequest,
    PayoutDestinationSet,
    PhoneVerificationConfirm,
    ReferredUserRead,
    RoundUpSettingsSet,
    Token,
    TransactionLimitsRead,
    UserCreate,
    UserLogin,
    UserRead,
)
from src.auth.utils import create_access_token, verify_password
from src.common import kyc_limits
from src.common.rate_limit import check_rate_limit
from src.db.main import get_session
from src.vaults.models import Vault

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(user_data: UserCreate, request: Request, session: AsyncSession = Depends(get_session)):
    # By IP, not phone number - the phone number being registered is the
    # thing under attack (mass account creation), so it can't be the key.
    check_rate_limit(f"register:{_client_ip(request)}", limit=5, window_seconds=3600)

    existing = await service.get_user_by_phone(session, user_data.phone_number)
    if existing:
        raise HTTPException(status_code=400, detail="Phone number already registered")

    user = await service.create_user(session, user_data)
    return user


@router.post("/login", response_model=Token)
async def login(credentials: UserLogin, session: AsyncSession = Depends(get_session)):
    # By phone number - the account being targeted, regardless of which IP
    # a credential-stuffing attempt happens to come from.
    check_rate_limit(f"login:{credentials.phone_number}", limit=10, window_seconds=300)

    user = await service.get_user_by_phone(session, credentials.phone_number)
    if not user or not verify_password(credentials.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid phone number or password")

    # Only revealed once the password is already confirmed correct - a
    # wrong-password attempt still gets the generic message above, so this
    # can't be used to probe whether a given number ever had an account.
    if user.closed_at is not None:
        raise HTTPException(status_code=403, detail="This account has been closed")

    access_token = create_access_token(user.id, user.token_version)
    return Token(access_token=access_token)


@router.post("/password-reset/request", status_code=status.HTTP_202_ACCEPTED)
async def request_password_reset(
    payload: PasswordResetRequest, request: Request, session: AsyncSession = Depends(get_session)
):
    # Double-keyed: by phone number (don't SMS-bomb one person) and by IP
    # (don't let one attacker cost the platform SMS fees by sweeping
    # through many numbers). Both checks run before touching the DB or SMS.
    check_rate_limit(f"password-reset-phone:{payload.phone_number}", limit=3, window_seconds=3600)
    check_rate_limit(f"password-reset-ip:{_client_ip(request)}", limit=10, window_seconds=3600)

    await service.request_password_reset(session, payload.phone_number)
    # Deliberately the same response whether or not that phone number has
    # an account - see the docstring on service.request_password_reset.
    return {"message": "If that phone number is registered, a reset code has been sent."}


@router.post("/password-reset/confirm", response_model=Token)
async def confirm_password_reset(payload: PasswordResetConfirm, session: AsyncSession = Depends(get_session)):
    user = await service.confirm_password_reset(session, payload.phone_number, payload.code, payload.new_password)
    access_token = create_access_token(user.id, user.token_version)
    return Token(access_token=access_token)


@router.post("/verify-phone/request", status_code=status.HTTP_202_ACCEPTED)
async def request_phone_verification(
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    check_rate_limit(f"verify-phone:{current_user.id}", limit=3, window_seconds=3600)
    await service.request_phone_verification(session, current_user)
    return {"message": "Verification code sent."}


@router.post("/verify-phone/confirm", response_model=UserRead)
async def confirm_phone_verification(
    payload: PhoneVerificationConfirm,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    return await service.confirm_phone_verification(session, current_user, payload.code)


@router.post("/me/close-account", status_code=status.HTTP_204_NO_CONTENT)
async def close_account(
    payload: AccountClosureRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if not verify_password(payload.password, current_user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect password")
    await service.close_account(session, current_user)


@router.get("/me", response_model=UserRead)
async def read_current_user(current_user: User = Depends(get_current_user)):
    return current_user


@router.get("/me/limits", response_model=TransactionLimitsRead)
async def my_limits(current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Verification tier, its daily/monthly limits and what's been used."""
    return await kyc_limits.limits_summary(session, current_user)


@router.get("/me/referrals", response_model=list[ReferredUserRead])
async def my_referrals(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    return await service.list_my_referrals(session, current_user.id)


@router.put("/me/payout-destination", response_model=UserRead)
async def set_payout_destination(
    payload: PayoutDestinationSet,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    current_user.default_momo_number = payload.momo_number
    current_user.default_momo_bank_code = payload.momo_bank_code
    current_user.default_account_name = payload.account_name
    session.add(current_user)
    await session.commit()
    await session.refresh(current_user)
    return current_user


@router.put("/me/round-up-settings", response_model=UserRead)
async def set_round_up_settings(
    payload: RoundUpSettingsSet,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if payload.vault_id is not None:
        vault = await session.get(Vault, payload.vault_id)
        if vault is None or vault.owner_id != current_user.id:
            raise HTTPException(status_code=404, detail="Vault not found")

    current_user.round_up_vault_id = payload.vault_id
    current_user.round_up_denomination = payload.denomination
    session.add(current_user)
    await session.commit()
    await session.refresh(current_user)
    return current_user
