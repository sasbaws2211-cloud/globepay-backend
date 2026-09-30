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
    SplitWithdrawalRead,
    SplitWithdrawRequest,
)
from src.vaults.models import Vault

router = APIRouter(prefix="/splits", tags=["splitbill"])


async def _withdrawal_read(session: AsyncSession, withdrawal) -> SplitWithdrawalRead | None:
    if withdrawal is None:
        return None
    vault = await session.get(Vault, withdrawal.vault_id) if withdrawal.vault_id else None
    return SplitWithdrawalRead(**withdrawal.model_dump(), vault_name=vault.name if vault else None)


async def _to_read(session: AsyncSession, bill, viewer_id=None) -> SplitBillRead:
    shares = await service.get_shares(session, bill.id)
    # What was collected and how it was withdrawn is the organizer's business only.
    is_organizer = viewer_id is None or viewer_id == bill.organizer_id
    gross, collected, withdrawal = None, None, None
    if bill.collects_funds and is_organizer:
        gross = service.collected_gross(bill, shares)
        collected = service.collected_amount(bill, shares)
        withdrawal = await _withdrawal_read(session, await service.get_withdrawal(session, bill.id))
    reads = []
    for s in shares:
        person = await session.get(User, s.user_id)
        reads.append(
            ShareRead(
                **s.model_dump(),
                participant_name=person.full_name if person else None,
                is_organizer=s.user_id == bill.organizer_id,
            )
        )
    # Organizer's own portion first, then participants.
    reads.sort(key=lambda r: not r.is_organizer)
    return SplitBillRead(
        id=bill.id,
        organizer_id=bill.organizer_id,
        title=bill.title,
        total_amount=bill.total_amount,
        status=bill.status,
        created_at=bill.created_at,
        shares=reads,
        collects_funds=bill.collects_funds,
        collected_gross_amount=gross,
        collected_amount=collected,
        withdrawal=withdrawal,
    )


@router.post("", response_model=SplitBillRead, status_code=status.HTTP_201_CREATED)
async def create_split(
    data: SplitBillCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    bill = await service.create_split_bill(session, current_user, data)
    return await _to_read(session, bill, current_user.id)


@router.get("", response_model=list[SplitBillRead])
async def my_splits(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Bills I organized or owe a share in."""
    return [
        await _to_read(session, b, current_user.id) for b in await service.list_my_split_bills(session, current_user.id)
    ]


@router.get("/{split_bill_id}", response_model=SplitBillRead)
async def get_split(
    split_bill_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    bill = await service.get_visible_split_bill(session, split_bill_id, current_user.id)
    return await _to_read(session, bill, current_user.id)


@router.post("/{split_bill_id}/cancel", response_model=SplitBillRead)
async def cancel_split(
    split_bill_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    bill = await service.get_split_bill(session, split_bill_id)
    bill = await service.cancel_split_bill(session, bill, current_user.id)
    return await _to_read(session, bill, current_user.id)


@router.post("/{split_bill_id}/close", response_model=SplitBillRead)
async def close_split(
    split_bill_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Stop collecting: drop the shares still owed and make what's been
    collected withdrawable. For when someone isn't going to pay."""
    bill = await service.close_split_bill(session, split_bill_id, current_user.id)
    return await _to_read(session, bill, current_user.id)


@router.post("/{split_bill_id}/withdraw", response_model=SplitBillRead)
async def withdraw_split(
    split_bill_id: uuid.UUID,
    payload: SplitWithdrawRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Withdraw a settled bill's collected money to mobile money or a vault.
    Also retries a failed withdrawal (either destination)."""
    await service.withdraw(session, split_bill_id, current_user, payload.destination, payload.vault_id)
    bill = await service.get_split_bill(session, split_bill_id)
    await session.refresh(bill)
    return await _to_read(session, bill, current_user.id)


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
