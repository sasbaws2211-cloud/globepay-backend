import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.models import User
from src.common import bitnob_balance, kyc_limits
from src.common.locking import locked_first
from src.common.sms import send_sms
from src.config import settings
from src.crossborder import bitnob, corridors
from src.crossborder.models import CrossBorderStatus, CrossBorderTransfer
from src.crossborder.schemas import CrossBorderInitiate
from src.payments import paystack, refunds

MAX_DELIVERY_RETRIES = 3

logger = logging.getLogger(__name__)


def _amount_usdc(source_amount: Decimal) -> Decimal:
    # Bitnob's quote API wants an amount already in USD terms; our sender
    # pays in GHS via Paystack, so this bridges the gap. Illustrative only
    # (settings.DEMO_GHS_USD_RATE), not a live rate feed.
    return (source_amount / Decimal(str(settings.DEMO_GHS_USD_RATE))).quantize(Decimal("0.01"))


async def _new_quote(transfer: CrossBorderTransfer, reference: str) -> Decimal:
    """Get a Bitnob quote and store its id/rate/amount on the transfer.
    Returns what the payout will debit from our Bitnob float (USDC, fees in)."""
    quote = await bitnob.create_quote(
        from_asset="USDC",  # matches the funded sandbox test balance
        to_currency=transfer.destination_currency,
        country=transfer.destination_country,
        amount=str(_amount_usdc(transfer.source_amount)),
        reference=reference,
    )
    payout_data = quote.get("data", {}).get("payout", {})
    destination_amount = Decimal(str(payout_data.get("settlement_amount", "0")))
    rate = Decimal(str(payout_data.get("exchange_rate", {}).get("effective_rate", "0")))
    transfer.quote_id = payout_data.get("quote_id")
    transfer.destination_amount = destination_amount if destination_amount > 0 else None
    transfer.exchange_rate_used = rate if rate > 0 else None
    return Decimal(str(payout_data.get("total_amount") or payout_data.get("amount") or "0"))


async def _initialize(transfer: CrossBorderTransfer, reference: str) -> None:
    """Bitnob's initialize step. It validates the beneficiary (name, number,
    network, destination type) but moves no money - confirmed live."""
    init_result = await bitnob.initialize_payout(
        quote_id=transfer.quote_id,
        reference=reference,
        payment_reason="Cross-border transfer (sandbox demo)",
        beneficiary=transfer.beneficiary_details,
    )
    init_payout = init_result.get("data", {}).get("payout", {})
    transfer.bitnob_id = init_payout.get("id")
    transfer.bitnob_status = init_payout.get("status")


async def initiate_transfer(session: AsyncSession, sender: User, data: CrossBorderInitiate) -> dict:
    await kyc_limits.check_transaction_limit(session, sender, data.source_amount)

    # Check the beneficiary against Bitnob's own live requirements for this
    # corridor (country + currency + destination type) - every field, format
    # and option - before anything is quoted or charged.
    try:
        beneficiary, dest_spec = await corridors.build_beneficiary(
            data.destination_country, data.destination_currency, data.destination_type, data.beneficiary
        )
    except bitnob.BitnobError as e:
        raise HTTPException(status_code=503, detail=f"Couldn't load payout requirements: {e.user_message()}") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    reference = f"xborder-{uuid.uuid4().hex[:14]}"
    transfer = CrossBorderTransfer(
        sender_id=sender.id,
        source_amount=data.source_amount,
        destination_country=data.destination_country,
        destination_currency=data.destination_currency,
        beneficiary_details=beneficiary,
        payment_reference=reference,
    )

    # Quote AND initialize before taking any money. Initialize is where Bitnob
    # validates the beneficiary; it used to run only after payment, so a bad
    # network name, blank account number or unsupported destination (e.g. US
    # mobile money) charged the sender first and failed afterwards - confirmed
    # live with "M-Pesa". Only finalize (the step that moves money) waits for
    # the payment now.
    try:
        debit = await _new_quote(transfer, reference)
        # Bitnob's per-corridor min/max (destination currency), e.g. EUR 10-86,000.
        corridors.check_limits(dest_spec, transfer.destination_amount, data.destination_currency)
        # Every payout is paid from our Bitnob float. If it can't cover this
        # one, finalize fails AFTER the sender has paid ("Insufficient funds",
        # seen live on a GHS 500 -> EUR transfer against 13.3 USDC) - refuse
        # up front instead.
        float_available = await bitnob.available_balance("USDC")
        if float_available is not None and debit > float_available:
            logger.warning("Bitnob USDC float %s can't cover payout of %s (%s)", float_available, debit, reference)
            raise HTTPException(
                status_code=503,
                detail="International transfers of this size are temporarily unavailable. Try a smaller amount or try again later.",
            )
        await _initialize(transfer, f"{reference}-init")
    except bitnob.BitnobError as e:
        raise HTTPException(status_code=400, detail=e.user_message()) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if data.save_sender_profile and isinstance(beneficiary.get("sender"), dict):
        # Merged, not replaced: corridors ask for different sender fields (the
        # US adds state and ID), so an EU transfer shouldn't wipe those.
        sender.sender_profile = {**(sender.sender_profile or {}), **beneficiary["sender"]}
        session.add(sender)

    session.add(transfer)
    await session.commit()

    checkout = await paystack.initialize_transaction(
        email=data.sender_email,
        amount=data.source_amount,
        reference=reference,
        metadata={"type": "crossborder_transfer", "transfer_id": str(transfer.id)},
    )
    return {"authorization_url": checkout["authorization_url"], "reference": reference}


def _apply_settled_status(transfer: CrossBorderTransfer, status: str) -> None:
    transfer.bitnob_status = status
    if status.upper() == "SUCCESS":
        transfer.status = CrossBorderStatus.COMPLETED
        transfer.completed_at = datetime.now(timezone.utc)
    elif status.upper() == "FAILED":
        transfer.status = CrossBorderStatus.DELIVERY_FAILED
        transfer.failure_reason = "Bitnob payout settlement failed"
    else:
        # Still settling - reconcile_processing (the sweep) finishes it.
        transfer.status = CrossBorderStatus.PROCESSING


async def _finalize_and_settle(transfer: CrossBorderTransfer) -> None:
    """finalize is the step that moves money; it raises BitnobError if Bitnob
    rejects it. Once it has succeeded, a failure while *checking* the result
    must NOT be treated as a failed payout (a re-quote would send it twice),
    so status checks here never raise - an unknown outcome stays PROCESSING."""
    final_result = await bitnob.finalize_payout(quote_id=transfer.quote_id)
    # Money has left the float - remembered so Bitnob's post-debit 0 reading
    # doesn't refuse the next transfer (src/common/bitnob_balance.py).
    bitnob_balance.record_debit("USDC", _amount_usdc(transfer.source_amount))
    final_status = final_result.get("data", {}).get("payout", {}).get("status", "")
    transfer.bitnob_status = final_status

    # finalize returns PENDING immediately and settles to SUCCESS/FAILED
    # asynchronously - confirmed ~150ms in sandbox, so a few short polls
    # usually catch the real outcome.
    if final_status.upper() == "PENDING" and transfer.bitnob_id:
        for _ in range(5):
            await asyncio.sleep(1)
            try:
                status_result = await bitnob.get_payout(transfer.bitnob_id)
            except bitnob.BitnobError:
                break  # outcome unknown - leave it for the sweep
            polled_status = status_result.get("data", {}).get("payout", {}).get("status", "")
            if polled_status.upper() != "PENDING":
                final_status = polled_status
                break
    _apply_settled_status(transfer, final_status)


async def _attempt_delivery(transfer: CrossBorderTransfer) -> None:
    """Mutates transfer.status/bitnob_*/failure_reason/completed_at in
    place. Never raises: the GHS payment has already been collected by the
    time this runs (both on first attempt and on retry), so any failure
    here must land in DELIVERY_FAILED - a retryable/refundable state -
    rather than bubble up and be lost."""
    try:
        try:
            if not transfer.bitnob_id:
                # Transfers started before initialize moved ahead of payment.
                await _initialize(transfer, f"{transfer.payment_reference}-init")
            await _finalize_and_settle(transfer)
        except bitnob.BitnobError as first_error:
            # Nothing was sent (finalize itself was rejected) - most often the
            # quote expired while the sender was paying, or on a later Retry
            # (which used to reuse the stale quote and fail every time). Get a
            # fresh quote and try once more.
            logger.info("Cross-border %s: finalize rejected (%s) - re-quoting", transfer.id, first_error.user_message())
            attempt = uuid.uuid4().hex[:6]
            await _new_quote(transfer, f"{transfer.payment_reference}-q{attempt}")
            await _initialize(transfer, f"{transfer.payment_reference}-init-{attempt}")
            await _finalize_and_settle(transfer)
    except bitnob.BitnobError as e:
        # GHS payment already succeeded but the Bitnob sandbox leg failed -
        # "paid but not delivered". DELIVERY_FAILED lets the sender retry
        # (retry_delivery) or get their GHS payment refunded
        # (refund_delivery_failure) instead of the money just vanishing.
        transfer.status = CrossBorderStatus.DELIVERY_FAILED
        transfer.failure_reason = f"Bitnob sandbox call failed: {e.user_message()}"


async def reconcile_processing(session: AsyncSession, transfer_id: uuid.UUID) -> bool:
    """Background sweep: settle a PROCESSING transfer from Bitnob's real
    payout status (these used to stay "processing" forever). True if it moved."""
    transfer = await locked_first(session, CrossBorderTransfer, CrossBorderTransfer.id == transfer_id)
    if transfer is None or transfer.status != CrossBorderStatus.PROCESSING or not transfer.bitnob_id:
        await session.rollback()
        return False
    result = await bitnob.get_payout(transfer.bitnob_id)
    status = result.get("data", {}).get("payout", {}).get("status", "")
    if status.upper() in ("", "PENDING", "PROCESSING", "INITIATED"):
        await session.rollback()
        return False
    _apply_settled_status(transfer, status)
    session.add(transfer)
    await session.commit()
    sender = await session.get(User, transfer.sender_id)
    if transfer.status == CrossBorderStatus.COMPLETED:
        await send_sms(sender.phone_number, f"Your cross-border transfer of GHS {transfer.source_amount} has been delivered.")
    elif transfer.status == CrossBorderStatus.DELIVERY_FAILED:
        await send_sms(sender.phone_number, f"Your cross-border transfer of GHS {transfer.source_amount} couldn't be "
                       f"delivered. Open the app to retry or get a refund.")
    return True


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

    kyc_limits.record_transaction_volume(session, transfer.sender_id, transfer.source_amount, "crossborder_transfer")
    await _attempt_delivery(transfer)

    session.add(transfer)
    await session.commit()
    await session.refresh(transfer)
    return transfer


async def retry_delivery(session: AsyncSession, transfer: CrossBorderTransfer) -> CrossBorderTransfer:
    # Locked: a double-tapped Retry must not run delivery (and send money) twice.
    transfer = await locked_first(session, CrossBorderTransfer, CrossBorderTransfer.id == transfer.id)
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
