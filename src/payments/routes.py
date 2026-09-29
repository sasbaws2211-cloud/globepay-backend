import hashlib
import hmac

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel.ext.asyncio.session import AsyncSession

from src.cards import service as card_service
from src.config import settings
from src.crossborder import service as crossborder_service
from src.db.main import get_session
from src.payments import refunds
from src.splitbill import service as splitbill_service
from src.vaults import service as vault_service
from src.wallet import service as wallet_service

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _verify_signature(raw_body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    expected = hmac.new(
        settings.PAYSTACK_SECRET_KEY.encode("utf-8"), raw_body, hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/paystack")
async def paystack_webhook(request: Request, session: AsyncSession = Depends(get_session)):
    raw_body = await request.body()
    signature = request.headers.get("x-paystack-signature")

    if not _verify_signature(raw_body, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = await request.json()
    event = payload.get("event")
    data = payload.get("data", {})

    if event == "charge.success":
        metadata = data.get("metadata", {})
        reference = data.get("reference")
        if metadata.get("type") == "vault_contribution":
            await vault_service.confirm_contribution(session, reference)
        elif metadata.get("type") == "wallet_transfer":
            await wallet_service.confirm_transfer_payment(session, reference)
        elif metadata.get("type") == "splitbill_share":
            await splitbill_service.confirm_share_payment(session, reference)
        elif metadata.get("type") == "crossborder_transfer":
            await crossborder_service.confirm_transfer_payment(session, reference)
        elif metadata.get("type") == "card_creation":
            await card_service.confirm_card_payment(session, reference)
        elif metadata.get("type") == "card_funding":
            await card_service.confirm_card_funding(session, reference)

    elif event in ("transfer.success", "transfer.failed", "transfer.reversed"):
        # Outbound payouts carry no metadata, so route by reference prefix.
        reference = data.get("reference") or ""
        if reference.startswith(wallet_service.PAYOUT_REFERENCE_PREFIX):
            await wallet_service.handle_payout_event(session, event, reference)
        elif reference.startswith(vault_service.VAULT_PAYOUT_REFERENCE_PREFIX):
            await vault_service.handle_payout_event(session, event, reference)
        elif reference.startswith(splitbill_service.SPLIT_PAYOUT_REFERENCE_PREFIX):
            await splitbill_service.handle_payout_event(session, event, reference)

    elif event in ("refund.processed", "refund.failed"):
        # A refund only counts as done once Paystack actually pays it out.
        await refunds.handle_refund_webhook(session, event, data)

    return {"received": True}
