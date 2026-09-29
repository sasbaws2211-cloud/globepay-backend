from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.dependencies import get_current_user
from src.auth.models import User
from src.db.main import get_session
from src.payments.reconcile import refresh_charge

router = APIRouter(prefix="/payments", tags=["payments"])


class ChargeRefreshRead(BaseModel):
    kind: str  # vault_contribution | splitbill_share | crossborder_transfer | card_creation | card_funding
    status: str
    pending: bool  # true while Paystack still hasn't reported a final charge state


@router.post("/{reference}/refresh", response_model=ChargeRefreshRead)
async def refresh_payment(
    reference: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Polled by the app after a Paystack checkout closes, so a payment
    confirms even when the webhook is late or can't reach this server.
    Wallet transfers have their own /wallet/transfers/{id}/refresh."""
    result = await refresh_charge(session, reference, current_user.id)
    if result is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    return result
