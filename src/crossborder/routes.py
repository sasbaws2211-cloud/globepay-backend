import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.dependencies import get_current_user
from src.auth.models import User
from src.common.bitnob_client import BitnobError
from src.common.idempotency import run_idempotently
from src.crossborder import corridors, service
from src.crossborder.schemas import CrossBorderInitiate, CrossBorderInitiateResponse, CrossBorderRead, SenderProfile
from src.db.main import get_session

router = APIRouter(prefix="/crossborder", tags=["crossborder (sandbox demo only)"])


@router.post("/transfers", response_model=CrossBorderInitiateResponse)
async def initiate_transfer(
    data: CrossBorderInitiate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    async def _handler() -> dict:
        return await service.initiate_transfer(session, current_user, data)

    if idempotency_key:
        return await run_idempotently(
            session, current_user.id, idempotency_key, "POST /crossborder/transfers",
            data.model_dump(mode="json"), _handler,
        )
    return await _handler()


def _mask(value: str | None) -> str | None:
    if not value:
        return None
    return f"•••• {value[-4:]}" if len(value) > 4 else value


async def _to_read(transfer) -> dict:
    details = transfer.beneficiary_details or {}
    return {
        **transfer.model_dump(),
        "destination_type": details.get("destination_type"),
        # US ACH/wire keep the recipient's name inside the nested beneficiary block.
        "beneficiary_name": details.get("account_name") or (details.get("beneficiary") or {}).get("account_name"),
        # Nigeria bank payouts carry no recipient name at all - the bank is what identifies them.
        "beneficiary_bank": await corridors.bank_name(details),
        "beneficiary_account": _mask(details.get("account_number")),
    }


@router.get("/corridors")
async def list_corridors(current_user: User = Depends(get_current_user)):
    """Countries Bitnob can pay out to, each with its currencies and delivery
    methods. Drives the transfer form's country/currency/method choices."""
    try:
        return await corridors.list_countries()
    except BitnobError as e:
        raise HTTPException(status_code=503, detail=f"Couldn't load payout countries: {e.user_message()}") from e


@router.get("/corridors/{country}")
async def corridor_requirements(country: str, current_user: User = Depends(get_current_user)):
    """Bitnob's field-by-field requirements for each delivery method to this
    country - the form renders these, the backend validates against them."""
    try:
        return await corridors.get_requirements(country.upper())
    except BitnobError as e:
        raise HTTPException(status_code=502, detail=e.user_message()) from e


@router.get("/corridors/{country}/limits")
async def corridor_limits(
    country: str, currency: str, destination_type: str, current_user: User = Depends(get_current_user)
):
    """Min/max for this payout, in the destination currency and roughly in
    GHS, so the form can show them before the sender tries to pay."""
    try:
        return await corridors.amount_limits(country.upper(), currency.upper(), destination_type)
    except BitnobError as e:
        raise HTTPException(status_code=502, detail=e.user_message()) from e


@router.get("/sender-profile", response_model=SenderProfile)
async def sender_profile(current_user: User = Depends(get_current_user)):
    return {"sender": current_user.sender_profile}


@router.get("/transfers", response_model=list[CrossBorderRead])
async def my_transfers(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    return [await _to_read(t) for t in await service.list_my_transfers(session, current_user.id)]


@router.get("/transfers/{transfer_id}", response_model=CrossBorderRead)
async def get_transfer(
    transfer_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    return await _to_read(await service.get_transfer(session, transfer_id, current_user.id))


@router.post("/transfers/{transfer_id}/retry", response_model=CrossBorderRead)
async def retry_transfer(
    transfer_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    transfer = await service.get_transfer(session, transfer_id, current_user.id)
    return await _to_read(await service.retry_delivery(session, transfer))


@router.post("/transfers/{transfer_id}/refund", response_model=CrossBorderRead)
async def refund_transfer(
    transfer_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    transfer = await service.get_transfer(session, transfer_id, current_user.id)
    return await _to_read(await service.refund_delivery_failure(session, transfer))
