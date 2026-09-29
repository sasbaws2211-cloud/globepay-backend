import uuid

from fastapi import APIRouter, Depends, Header, status
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.dependencies import get_current_user
from src.auth.models import User
from src.common.idempotency import run_idempotently
from src.db.main import get_session
from src.splitbill import service
from src.splitbill.schemas import (
    ShareRead,
    SharePayInitiate,
    SharePayInitiateResponse,
    SplitBillCreate,
    SplitBillRead,
)

router = APIRouter(prefix="/splits", tags=["splitbill"])


async def _to_read(session: AsyncSession, bill) -> SplitBillRead:
    shares = await service.get_shares(session, bill.id)
    return SplitBillRead(
        id=bill.id,
        organizer_id=bill.organizer_id,
        title=bill.title,
        total_amount=bill.total_amount,
        status=bill.status,
        created_at=bill.created_at,
        shares=[ShareRead(**s.model_dump()) for s in shares],
    )


@router.post("", response_model=SplitBillRead, status_code=status.HTTP_201_CREATED)
async def create_split(
    data: SplitBillCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    bill = await service.create_split_bill(session, current_user, data)
    return await _to_read(session, bill)


@router.get("/{split_bill_id}", response_model=SplitBillRead)
async def get_split(split_bill_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    bill = await service.get_split_bill(session, split_bill_id)
    return await _to_read(session, bill)


@router.post("/{split_bill_id}/cancel", response_model=SplitBillRead)
async def cancel_split(
    split_bill_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    bill = await service.get_split_bill(session, split_bill_id)
    bill = await service.cancel_split_bill(session, bill, current_user.id)
    return await _to_read(session, bill)


@router.post("/{split_bill_id}/shares/{share_id}/retry-payout", response_model=ShareRead)
async def retry_share_payout(
    split_bill_id: uuid.UUID,
    share_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    bill = await service.get_split_bill(session, split_bill_id)
    share = await service.retry_share_payout(session, bill, share_id, current_user.id)
    return ShareRead(**share.model_dump())


@router.get("/pending/me", response_model=list[ShareRead])
async def my_pending_shares(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    shares = await service.list_my_pending_shares(session, current_user.id)
    return [ShareRead(**s.model_dump()) for s in shares]


@router.post("/{split_bill_id}/pay", response_model=SharePayInitiateResponse)
async def pay_my_share(
    split_bill_id: uuid.UUID,
    payload: SharePayInitiate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    share = await service.get_my_share(session, split_bill_id, current_user.id)

    async def _handler() -> dict:
        return await service.initiate_share_payment(session, share, payload.email)

    if idempotency_key:
        return await run_idempotently(
            session, current_user.id, idempotency_key, f"POST /splits/{split_bill_id}/pay",
            payload.model_dump(mode="json"), _handler,
        )
    return await _handler()
