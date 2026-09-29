import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.models import User
from src.common.kyc_limits import check_transaction_limit, record_transaction_volume
from src.common.locking import locked_first
from src.common.sms import send_sms
from src.config import settings
from src.crossborder import bitnob
from src.crossborder.models import CrossBorderStatus, CrossBorderTransfer
from src.crossborder.schemas import CrossBorderInitiate
from src.payments import paystack, refunds

MAX_DELIVERY_RETRIES = 3


async def initiate_transfer(session: AsyncSession, sender: User, data: CrossBorderInitiate) -> dict:
    await check_transaction_limit(session, sender, data.source_amount)

    # Bitnob's quote API wants an amount already in USD terms; our sender
    # pays in GHS via Paystack, so this bridges the gap. Illustrative only
    # (settings.DEMO_GHS_USD_RATE), not a live rate feed.
    amount_usdc = (data.source_amount / Decimal(str(settings.DEMO_GHS_USD_RATE))).quantize(Decimal("0.01"))

    reference = f"xborder-{uuid.uuid4().hex[:14]}"

    try:
        quote = await bitnob.create_quote(
            from_asset="USDC",  # matches the funded sandbox test balance
            to_currency=data.destination_currency,
            country=data.destination_country,
            amount=str(amount_usdc),
            reference=reference,
        )
    except bitnob.BitnobError as e:
        raise HTTPException(status_code=400, detail=e.user_message()) from e

    payout_data = quote.get("data", {}).get("payout", {})
    destination_amount = Decimal(str(payout_data.get("settlement_amount", "0")))
    rate = Decimal(str(payout_data.get("exchange_rate", {}).get("effective_rate", "0")))

    transfer = CrossBorderTransfer(
        sender_id=sender.id,
        source_amount=data.source_amount,
        destination_country=data.destination_country,
        destination_currency=data.destination_currency,
        destination_amount=destination_amount if destination_amount > 0 else None,
        exchange_rate_used=rate if rate > 0 else None,
        quote_id=payout_data.get("quote_id"),
        # Bitnob's beneficiary object requires `country`, redundant with
        # destination_country already collected above - auto-filled here
        # rather than asked for twice. Missing this caused a real 400 on
        # the initialize step, only caught by testing with a real quote_id.
        beneficiary_details={**data.beneficiary.model_dump(), "country": data.destination_country},
        payment_reference=reference,
    )
    session.add(transfer)
    await session.commit()

    checkout = await paystack.initialize_transaction(
        email=data.sender_email,
        amount=data.source_amount,
        reference=reference,
        metadata={"type": "crossborder_transfer", "transfer_id": str(transfer.id)},
    )
    return {"authorization_url": checkout["authorization_url"], "reference": reference}


async def _attempt_delivery(transfer: CrossBorderTransfer) -> None:
    """Mutates transfer.status/bitnob_*/failure_reason/completed_at in
    place. Never raises: the GHS payment has already been collected by the
    time this runs (both on first attempt and on retry), so any failure
    here must land in DELIVERY_FAILED - a retryable/refundable state -
    rather than bubble up and be lost."""
    try:
        init_result = await bitnob.initialize_payout(
            quote_id=transfer.quote_id,
            reference=f"{transfer.payment_reference}-init",
            payment_reason="Cross-border transfer (sandbox demo)",
            beneficiary=transfer.beneficiary_details,
        )
        init_payout = init_result.get("data", {}).get("payout", {})
        transfer.bitnob_id = init_payout.get("id")
        transfer.bitnob_status = init_payout.get("status")

        final_result = await bitnob.finalize_payout(quote_id=transfer.quote_id)
        final_status = final_result.get("data", {}).get("payout", {}).get("status", "")
        transfer.bitnob_status = final_status

        # finalize returns PENDING immediately and settles to
        # SUCCESS/FAILED asynchronously - confirmed ~150ms in sandbox
        # testing, so a few short polls is enough to catch the real
        # outcome instead of leaving every transfer stuck at PROCESSING.
        if final_status.upper() == "PENDING" and transfer.bitnob_id:
            for _ in range(5):
                await asyncio.sleep(1)
                status_result = await bitnob.get_payout(transfer.bitnob_id)
                polled_status = status_result.get("data", {}).get("payout", {}).get("status", "")
                if polled_status.upper() != "PENDING":
                    final_status = polled_status
                    transfer.bitnob_status = final_status
                    break

        if final_status.upper() == "SUCCESS":
            transfer.status = CrossBorderStatus.COMPLETED
            transfer.completed_at = datetime.now(timezone.utc)
        elif final_status.upper() == "FAILED":
            transfer.status = CrossBorderStatus.DELIVERY_FAILED
            transfer.failure_reason = "Bitnob payout settlement failed"
        else:
            transfer.status = CrossBorderStatus.PROCESSING
    except bitnob.BitnobError as e:
        # GHS payment already succeeded but the Bitnob sandbox leg failed -
        # "paid but not delivered". DELIVERY_FAILED lets the sender retry
        # (retry_delivery) or get their GHS payment refunded
        # (refund_delivery_failure) instead of the money just vanishing.
        transfer.status = CrossBorderStatus.DELIVERY_FAILED
        transfer.failure_reason = f"Bitnob sandbox call failed: {e.user_message()}"


async def confirm_transfer_payment(session: AsyncSession, reference: str) -> CrossBorderTransfer:
    """Called from the Paystack webhook handler once the GHS payment succeeds,
    and from the refresh/reconcile pollers - locked so delivery can't run twice."""
    transfer = await locked_first(session, CrossBorderTransfer, CrossBorderTransfer.payment_reference == reference)
    if transfer is None:
        raise HTTPException(status_code=404, detail="Transfer not found")

    if transfer.status != CrossBorderStatus.PENDING_PAYMENT:
        return transfer  # already processed

    verified = await paystack.verify_transaction(reference)
    if verified.get("status") != "success":
        transfer.status = CrossBorderStatus.FAILED
        transfer.failure_reason = "GHS payment not successful"
        session.add(transfer)
        await session.commit()
        return transfer

    record_transaction_volume(session, transfer.sender_id, transfer.source_amount, "crossborder_transfer")

    await _attempt_delivery(transfer)

    session.add(transfer)
    await session.commit()
    await session.refresh(transfer)
    return transfer


async def retry_delivery(session: AsyncSession, transfer: CrossBorderTransfer) -> CrossBorderTransfer:
    if transfer.status != CrossBorderStatus.DELIVERY_FAILED:
        raise HTTPException(
            status_code=400, detail="Only a transfer stuck after payment (delivery_failed) can be retried"
        )
    if transfer.retry_count >= MAX_DELIVERY_RETRIES:
        raise HTTPException(
            status_code=400,
            detail=f"Retry limit reached ({MAX_DELIVERY_RETRIES}) - request a refund instead",
        )

    transfer.retry_count += 1
    await _attempt_delivery(transfer)
    session.add(transfer)
    await session.commit()
    await session.refresh(transfer)

    if transfer.status == CrossBorderStatus.COMPLETED:
        sender = await session.get(User, transfer.sender_id)
        await send_sms(
            sender.phone_number,
            f"Good news - your cross-border transfer of GHS {transfer.source_amount} went through on retry.",
        )
    return transfer


async def refund_delivery_failure(session: AsyncSession, transfer: CrossBorderTransfer) -> CrossBorderTransfer:
    """Moves the transfer to REFUND_PENDING; REFUNDED only once Paystack
    reports the refund processed - see src/payments/refunds.py."""
    return await refunds.start_refund(session, refunds.CROSSBORDER, transfer.id)


async def get_transfer(session: AsyncSession, transfer_id: uuid.UUID, owner_id: uuid.UUID) -> CrossBorderTransfer:
    transfer = await session.get(CrossBorderTransfer, transfer_id)
    if transfer is None or transfer.sender_id != owner_id:
        raise HTTPException(status_code=404, detail="Transfer not found")
    return transfer


async def list_my_transfers(session: AsyncSession, sender_id: uuid.UUID) -> list[CrossBorderTransfer]:
    result = await session.exec(
        select(CrossBorderTransfer)
        .where(CrossBorderTransfer.sender_id == sender_id)
        .order_by(CrossBorderTransfer.created_at.desc())
    )
    return list(result.all())
