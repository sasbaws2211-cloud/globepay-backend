import uuid

from fastapi import APIRouter, Depends, Header
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.dependencies import get_current_user
from src.auth.models import User
from src.cards import service
from src.common.idempotency import run_idempotently
from src.cards.schemas import (
    CardTransactionRead,
    CardCreate,
    CardCreateResponse,
    CardFundingCreate,
    CardFundingRead,
    CardFundingResponse,
    CardLimitsRead,
    CardRead,
    CardTerminate,
)
from src.db.main import get_session

router = APIRouter(prefix="/cards", tags=["cards (sandbox demo only)"])


@router.post("", response_model=CardCreateResponse)
async def create_card(
    data: CardCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    async def _handler() -> dict:
        return await service.initiate_card_creation(session, current_user, data)

    if idempotency_key:
        return await run_idempotently(
            session, current_user.id, idempotency_key, "POST /cards", data.model_dump(mode="json"), _handler
        )
    return await _handler()


@router.get("", response_model=list[CardRead])
async def my_cards(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    return await service.list_my_cards(session, current_user.id)


# Declared before /{card_id} so "limits" isn't parsed as a card id.
@router.get("/limits", response_model=CardLimitsRead)
async def card_limits(current_user: User = Depends(get_current_user)):
    return service.get_card_limits()


@router.get("/{card_id}", response_model=CardRead)
async def get_card(
    card_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    card = await service.get_card(session, card_id, current_user.id)
    return await service.sync_card_status(session, card)


@router.get("/{card_id}/transactions", response_model=list[CardTransactionRead])
async def list_transactions(
    card_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    card = await service.get_card(session, card_id, current_user.id)
    return await service.list_card_transactions(session, card)


@router.get("/{card_id}/fundings", response_model=list[CardFundingRead])
async def list_fundings(
    card_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    card = await service.get_card(session, card_id, current_user.id)
    return await service.list_card_fundings(session, card)


@router.post("/{card_id}/freeze", response_model=CardRead)
async def freeze_card(
    card_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    card = await service.get_card(session, card_id, current_user.id)
    return await service.freeze_card(session, card)


@router.post("/{card_id}/unfreeze", response_model=CardRead)
async def unfreeze_card(
    card_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    card = await service.get_card(session, card_id, current_user.id)
    return await service.unfreeze_card(session, card)


@router.post("/{card_id}/terminate", response_model=CardRead)
async def terminate_card(
    card_id: uuid.UUID,
    data: CardTerminate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    card = await service.get_card(session, card_id, current_user.id)
    return await service.terminate_card(session, card, data)


@router.post("/{card_id}/fund", response_model=CardFundingResponse)
async def fund_card(
    card_id: uuid.UUID,
    data: CardFundingCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    card = await service.get_card(session, card_id, current_user.id)

    async def _handler() -> dict:
        return await service.initiate_card_funding(session, card, data)

    if idempotency_key:
        return await run_idempotently(
            session, current_user.id, idempotency_key, f"POST /cards/{card_id}/fund",
            data.model_dump(mode="json"), _handler,
        )
    return await _handler()


@router.post("/{card_id}/retry", response_model=CardRead)
async def retry_card_creation(
    card_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    card = await service.get_card(session, card_id, current_user.id)
    return await service.retry_card_creation(session, card)


@router.post("/{card_id}/refund", response_model=CardRead)
async def refund_card_creation(
    card_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    card = await service.get_card(session, card_id, current_user.id)
    return await service.refund_card_creation(session, card)


@router.post("/{card_id}/fundings/{funding_id}/retry", response_model=CardFundingRead)
async def retry_card_funding(
    card_id: uuid.UUID,
    funding_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    card = await service.get_card(session, card_id, current_user.id)
    funding = await service.get_card_funding(session, card, funding_id)
    return await service.retry_card_funding(session, card, funding)


@router.post("/{card_id}/fundings/{funding_id}/refund", response_model=CardFundingRead)
async def refund_card_funding(
    card_id: uuid.UUID,
    funding_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    card = await service.get_card(session, card_id, current_user.id)
    funding = await service.get_card_funding(session, card, funding_id)
    return await service.refund_card_funding(session, funding)
