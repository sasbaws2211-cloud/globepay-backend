import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, Response
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.dependencies import get_current_user
from src.auth.models import User
from src.cards import secure_details, service, termination_payout
from src.common.idempotency import run_idempotently
from src.cards.schemas import (
    CardTransactionRead,
    CardCreate,
    CardCreateResponse,
    CardQuoteRead,
    CardDetailsRead,
    CardDetailsRequest,
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


@router.get("/quote", response_model=CardQuoteRead)
async def card_quote(amount_ghs: Decimal, current_user: User = Depends(get_current_user)):
    """Price a new card before paying: the load, Bitnob's fees (passed on to
    the user) and the total charged."""
    return service.price_load(amount_ghs)


@router.get("/{card_id}", response_model=CardRead)
async def get_card(
    card_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    card = await service.get_card(session, card_id, current_user.id)
    return await service.sync_card_status(session, card)


@router.post("/{card_id}/details", response_model=CardDetailsRead)
async def reveal_card_details(
    card_id: uuid.UUID,
    data: CardDetailsRequest,
    response: Response,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Full card number, expiry and CVV (plus billing address) for the card's
    owner, after re-entering their password - e.g. to add it to a subscription.
    POST so the password travels in the body; never cached, logged or stored."""
    response.headers["Cache-Control"] = "no-store"
    card = await service.get_card(session, card_id, current_user.id)
    return await secure_details.reveal_card_details(current_user, card, data.password)


@router.get("/{card_id}/transactions", response_model=list[CardTransactionRead])
async def list_transactions(
    card_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    card = await service.get_card(session, card_id, current_user.id)
    return await service.list_card_transactions(session, card)


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


@router.post("/{card_id}/termination-payout/retry", response_model=CardRead)
async def retry_termination_payout(
    card_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Re-send a terminated card's leftover balance whose payout failed."""
    return await termination_payout.retry_termination_payout(session, card_id, current_user.id)


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
