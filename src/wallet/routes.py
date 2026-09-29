import uuid

from fastapi import APIRouter, Depends, Header
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.dependencies import get_current_user
from src.auth.models import User
from src.common.idempotency import run_idempotently
from src.db.main import get_session
from src.wallet import service
from src.wallet.schemas import TransferClaim, TransferInitiate, TransferInitiateResponse, TransferRead, WalletSummary

router = APIRouter(prefix="/wallet", tags=["wallet"])


@router.get("/summary", response_model=WalletSummary)
async def wallet_summary(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    return await service.get_wallet_summary(session, current_user.id)


@router.post("/transfers", response_model=TransferInitiateResponse)
async def send_money(
    payload: TransferInitiate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    async def _handler() -> dict:
        return await service.initiate_transfer(
            session,
            current_user,
            payload.recipient_phone_number,
            payload.amount,
            payload.note,
            payload.sender_email,
        )

    if idempotency_key:
        return await run_idempotently(
            session, current_user.id, idempotency_key, "POST /wallet/transfers",
            payload.model_dump(mode="json"), _handler,
        )
    return await _handler()


@router.get("/transfers", response_model=list[TransferRead])
async def my_transfers(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    return await service.list_my_transfers(session, current_user.id)


@router.get("/transfers/pending-claim", response_model=list[TransferRead])
async def incoming_pending_claim(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    return await service.list_incoming_pending(session, current_user.id)


@router.post("/transfers/{transfer_id}/refresh", response_model=TransferRead)
async def refresh_transfer(
    transfer_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    return await service.refresh_transfer(session, transfer_id, current_user.id)


@router.post("/transfers/{transfer_id}/claim", response_model=TransferRead)
async def claim_transfer(
    transfer_id: uuid.UUID,
    payload: TransferClaim,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    return await service.claim_transfer(session, transfer_id, current_user.id, payload)
