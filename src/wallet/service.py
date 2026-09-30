import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.models import User
from src.auth.service import get_user_by_phone
from src.common import kyc_limits
from src.common.roundup import compute_roundup
from src.common.sms import send_sms
from src.config import settings
from src.payments import paystack
from src.vaults.service import credit_roundup
from src.wallet.models import TransferStatus, WalletTransfer
from src.wallet.schemas import TransferClaim, WalletSummary

logger = logging.getLogger(__name__)

async def get_transfer(session: AsyncSession, transfer_id: uuid.UUID) -> WalletTransfer:
    transfer = await session.get(WalletTransfer, transfer_id)
    if transfer is None:
        raise HTTPException(status_code=404, detail="Transfer not found")
    return transfer


def mask_phone(phone: str) -> str:
    """"+233200000002" -> "+233 20 *** 0002" - enough to confirm, not to harvest."""
    if len(phone) >= 8:
        return f"{phone[:4]} {phone[4:6]} *** {phone[-4:]}" if phone.startswith("+") else f"{phone[:3]} *** {phone[-4:]}"
    return phone


def _short_name(full_name: str) -> str:
    """"Kojo Owusu" -> "Kojo O." - confirms the recipient without exposing a full name."""
    parts = full_name.split()
    return f"{parts[0]} {parts[-1][0]}." if len(parts) > 1 else full_name


async def _resolve_recipient(session: AsyncSession, sender: User, recipient_phone_number: str) -> User:
    recipient = await get_user_by_phone(session, recipient_phone_number)
    # A suspended or self-closed account can't log in to claim, so money sent
    # there would be stranded - treat it exactly like an unknown number
    # (same message, so this doesn't reveal the account's state).
    if recipient is None or not recipient.is_active or recipient.closed_at is not None:
        raise HTTPException(status_code=404, detail="No user found with that phone number")
    if recipient.id == sender.id:
        raise HTTPException(status_code=400, detail="Cannot send money to yourself")
    return recipient


def _amounts(sender: User, amount: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """(fee, net, roundup) - one place, so the preview can't disagree with the charge."""
    fee = (amount * Decimal(settings.PLATFORM_WITHDRAWAL_FEE_PERCENT) / Decimal(100)).quantize(Decimal("0.01"))
    roundup = Decimal("0.00")
    if sender.round_up_vault_id is not None:
        roundup = compute_roundup(amount, sender.round_up_denomination)
    return fee, amount - fee, roundup


async def quote_transfer(session: AsyncSession, sender: User, recipient_phone_number: str, amount: Decimal) -> dict:
    """The review step before paying: who it's going to, the fee, what they
    get, the round-up and the total charge. Runs the same checks as
    initiate_transfer so a quote that passes can be paid."""
    recipient = await _resolve_recipient(session, sender, recipient_phone_number)
    fee, net, roundup = _amounts(sender, amount)
    await kyc_limits.check_transaction_limit(session, sender, amount + roundup)
    return {
        "recipient_name": _short_name(recipient.full_name),
        "recipient_phone": mask_phone(recipient.phone_number),
        "amount": amount,
        "platform_fee": fee,
        "recipient_gets": net,
        "roundup_amount": roundup,
        "total_charge": amount + roundup,
    }


async def to_read(session: AsyncSession, transfer: WalletTransfer, viewer_id: uuid.UUID) -> dict:
    """TransferRead fields plus the viewer-relative ones the history needs."""
    sent = transfer.sender_id == viewer_id
    other = await session.get(User, transfer.recipient_id if sent else transfer.sender_id)
    return {
        **transfer.model_dump(),
        "direction": "sent" if sent else "received",
        "counterparty_name": other.full_name if other else None,
        "counterparty_phone": mask_phone(other.phone_number) if other else None,
        "pay_url": transfer.authorization_url
        if sent and transfer.status == TransferStatus.PENDING_PAYMENT
        else None,
    }


async def initiate_transfer(
    session: AsyncSession,
    sender: User,
    recipient_phone_number: str,
    amount: Decimal,
    note: str | None,
    sender_email: str,
) -> dict:
    recipient = await _resolve_recipient(session, sender, recipient_phone_number)
    fee, net, roundup = _amounts(sender, amount)
    # Before the checkout exists - the sender is charged the transfer plus their round-up.
    await kyc_limits.check_transaction_limit(session, sender, amount + roundup)

    reference = f"wallet-{uuid.uuid4().hex[:14]}"
    transfer = WalletTransfer(
        sender_id=sender.id,
        recipient_id=recipient.id,
        gross_amount=amount,
        platform_fee=fee,
        net_amount=net,
        note=note,
        roundup_amount=roundup,
        payment_reference=reference,
    )
    session.add(transfer)
    await session.commit()
    await session.refresh(transfer)

    try:
        data = await paystack.initialize_transaction(
            email=sender_email,
            amount=amount + roundup,  # sender pays the transfer plus their round-up in one charge
            reference=reference,
            metadata={"type": "wallet_transfer", "transfer_id": str(transfer.id)},
        )
    except paystack.PaystackError:
        transfer.status = TransferStatus.FAILED
        session.add(transfer)
        await session.commit()
        raise
    transfer.authorization_url = data["authorization_url"]
    session.add(transfer)
    await session.commit()
    return {"authorization_url": data["authorization_url"], "reference": reference, "transfer_id": str(transfer.id)}


PAYOUT_REFERENCE_PREFIX = "wallet-payout-"


async def _locked_transfer(session: AsyncSession, *where) -> WalletTransfer | None:
    """SELECT ... FOR UPDATE, so a duplicate webhook delivery or a double-tapped
    claim waits for the first to commit and then sees its new status.
    populate_existing forces a re-read even if the row is already in the
    session's identity map, otherwise the lock would return stale state."""
    result = await session.exec(
        select(WalletTransfer).where(*where).with_for_update().execution_options(populate_existing=True)
    )
    return result.first()


async def _payout_to_destination(
    transfer: WalletTransfer, momo_number: str, momo_bank_code: str, account_name: str
) -> None:
    """Request the payout and move the transfer to PAYOUT_PENDING. Paystack's
    response only means the request was accepted - delivery is confirmed (or
    failed/reversed) later via the transfer.* webhook, see handle_payout_event."""
    recipient_code = await paystack.create_transfer_recipient(
        name=account_name, account_number=momo_number, bank_code=momo_bank_code
    )
    # Unique per attempt: a failed/reversed payout sends the transfer back to
    # the recipient to re-claim, and Paystack rejects a reused reference.
    reference = f"{PAYOUT_REFERENCE_PREFIX}{transfer.id}-{uuid.uuid4().hex[:8]}"
    result = await paystack.initiate_transfer(
        amount=transfer.net_amount,
        recipient_code=recipient_code,
        reason=transfer.note or "Wallet transfer",
        reference=reference,
    )
    transfer.payout_reference = result.get("reference", reference)
    transfer.status = TransferStatus.PAYOUT_PENDING
    if result.get("status") == "otp":
        logger.warning(
            "Payout %s for wallet transfer %s is held for OTP - Transfer OTP is enabled on the "
            "Paystack account, so it won't be sent until finalized in the Paystack dashboard "
            "(or OTP is disabled for API transfers).",
            transfer.payout_reference, transfer.id,
        )


async def confirm_transfer_payment(session: AsyncSession, reference: str) -> WalletTransfer:
    """Called from the Paystack webhook handler once the sender's charge succeeds."""
    transfer = await _locked_transfer(session, WalletTransfer.payment_reference == reference)
    if transfer is None:
        raise HTTPException(status_code=404, detail="Transfer not found")

    if transfer.status != TransferStatus.PENDING_PAYMENT:
        return transfer  # already processed

    verified = await paystack.verify_transaction(reference)
    if verified.get("status") != "success":
        transfer.status = TransferStatus.FAILED
        session.add(transfer)
        await session.commit()
        return transfer

    sender = await session.get(User, transfer.sender_id)
    kyc_limits.record_transaction_volume(
        session, sender.id, transfer.gross_amount + transfer.roundup_amount, "wallet_transfer"
    )
    if transfer.roundup_amount > 0 and sender.round_up_vault_id is not None:
        await credit_roundup(session, sender.round_up_vault_id, transfer.roundup_amount, f"{reference}-roundup")

    recipient = await session.get(User, transfer.recipient_id)

    if recipient.default_momo_number and recipient.default_momo_bank_code:
        try:
            await _payout_to_destination(
                transfer,
                recipient.default_momo_number,
                recipient.default_momo_bank_code,
                recipient.default_account_name or recipient.full_name,
            )
            await send_sms(
                recipient.phone_number,
                f"You've received GHS {transfer.net_amount} from {sender.full_name} "
                f"and it's on its way to your mobile money wallet.",
            )
        except paystack.PaystackError as exc:
            # The charge is settled, so leave the transfer claimable instead of
            # returning a webhook error that can trigger duplicate processing.
            logger.warning("Auto-payout for wallet transfer %s failed: %s", transfer.id, exc)
            transfer.status = TransferStatus.AWAITING_RECIPIENT_PAYOUT_INFO
            await send_sms(
                recipient.phone_number,
                f"Your GHS {transfer.net_amount} transfer needs payout details before it can be delivered.",
            )
    else:
        transfer.status = TransferStatus.AWAITING_RECIPIENT_PAYOUT_INFO
        await send_sms(
            recipient.phone_number,
            f"{sender.full_name} sent you GHS {transfer.net_amount}. "
            f"Open the app to add your mobile money details and claim it.",
        )

    session.add(transfer)
    await session.commit()
    await session.refresh(transfer)
    return transfer


async def claim_transfer(
    session: AsyncSession, transfer_id: uuid.UUID, requester_id: uuid.UUID, payload: TransferClaim
) -> WalletTransfer:
    transfer = await _locked_transfer(session, WalletTransfer.id == transfer_id)
    if transfer is None:
        raise HTTPException(status_code=404, detail="Transfer not found")
    if transfer.recipient_id != requester_id:
        raise HTTPException(status_code=403, detail="Only the recipient can claim this transfer")
    if transfer.status != TransferStatus.AWAITING_RECIPIENT_PAYOUT_INFO:
        raise HTTPException(status_code=400, detail="This transfer is not awaiting payout info")

    try:
        await _payout_to_destination(transfer, payload.momo_number, payload.momo_bank_code, payload.account_name)
    except paystack.PaystackError as exc:
        # Nothing was sent - the transfer stays claimable so the recipient can
        # correct their details and try again.
        raise HTTPException(status_code=502, detail=f"Payout could not be started: {exc}") from exc
    session.add(transfer)

    if payload.save_as_default:
        recipient = await session.get(User, requester_id)
        recipient.default_momo_number = payload.momo_number
        recipient.default_momo_bank_code = payload.momo_bank_code
        recipient.default_account_name = payload.account_name
        session.add(recipient)

    await session.commit()
    await session.refresh(transfer)
    return transfer


async def handle_payout_event(session: AsyncSession, event: str, reference: str) -> WalletTransfer | None:
    """Paystack transfer.success / transfer.failed / transfer.reversed webhook
    for a wallet payout - the only place a transfer becomes COMPLETED."""
    transfer = await _locked_transfer(session, WalletTransfer.payout_reference == reference)
    if transfer is None or transfer.status != TransferStatus.PAYOUT_PENDING:
        return transfer  # unknown/stale reference or already processed (duplicate delivery)

    recipient = await session.get(User, transfer.recipient_id)
    if event == "transfer.success":
        transfer.status = TransferStatus.COMPLETED
        transfer.completed_at = datetime.now(timezone.utc)
        await send_sms(
            recipient.phone_number,
            f"GHS {transfer.net_amount} has been delivered to your mobile money wallet.",
        )
    else:
        # Failed or reversed: the money came back to the platform's Paystack
        # balance, so hand it back to the recipient to re-claim with
        # corrected details rather than leaving it marked as delivered.
        logger.warning("Payout %s for wallet transfer %s: %s", reference, transfer.id, event)
        transfer.status = TransferStatus.AWAITING_RECIPIENT_PAYOUT_INFO
        await send_sms(
            recipient.phone_number,
            f"We couldn't deliver your GHS {transfer.net_amount} transfer to your mobile money wallet. "
            f"Open the app to check your payout details and claim it again.",
        )

    session.add(transfer)
    await session.commit()
    await session.refresh(transfer)
    return transfer


async def reconcile_transfer(session: AsyncSession, transfer: WalletTransfer) -> None:
    """Ask Paystack directly for whatever this transfer is waiting on, for when
    the webhook is late, lost, or can't reach this server (e.g. local dev).
    Funnels into the same handlers the webhook uses, so their row locks and
    status checks make a poll racing a webhook harmless."""
    if transfer.status == TransferStatus.PENDING_PAYMENT and transfer.payment_reference:
        verified = await paystack.verify_transaction(transfer.payment_reference)
        # Only act on a final charge state: "abandoned"/"ongoing"/"pending"
        # checkouts can still be paid, so they must not be marked FAILED.
        if verified.get("status") == "success":
            await confirm_transfer_payment(session, transfer.payment_reference)
        elif verified.get("status") == "failed":
            locked = await _locked_transfer(session, WalletTransfer.id == transfer.id)
            if locked is not None and locked.status == TransferStatus.PENDING_PAYMENT:
                locked.status = TransferStatus.FAILED
                session.add(locked)
                await session.commit()
    elif transfer.status == TransferStatus.PAYOUT_PENDING and transfer.payout_reference:
        verified = await paystack.verify_transfer(transfer.payout_reference)
        event = paystack.TRANSFER_FINAL_EVENTS.get(verified.get("status"))
        if event:
            await handle_payout_event(session, event, transfer.payout_reference)


async def refresh_transfer(session: AsyncSession, transfer_id: uuid.UUID, user_id: uuid.UUID) -> WalletTransfer:
    """Polled by the app while a transfer is in flight."""
    transfer = await get_transfer(session, transfer_id)
    if user_id not in (transfer.sender_id, transfer.recipient_id):
        raise HTTPException(status_code=404, detail="Transfer not found")
    try:
        await reconcile_transfer(session, transfer)
    except paystack.PaystackError as exc:
        # e.g. an unknown/expired reference - report the current state rather
        # than failing the poll.
        logger.info("Refresh of wallet transfer %s: Paystack lookup failed: %s", transfer.id, exc)
        await session.rollback()
    await session.refresh(transfer)
    return transfer


async def list_incoming_pending(session: AsyncSession, user_id: uuid.UUID) -> list[WalletTransfer]:
    result = await session.exec(
        select(WalletTransfer).where(
            WalletTransfer.recipient_id == user_id,
            WalletTransfer.status == TransferStatus.AWAITING_RECIPIENT_PAYOUT_INFO,
        )
    )
    return list(result.all())


async def list_my_transfers(session: AsyncSession, user_id: uuid.UUID) -> list[WalletTransfer]:
    result = await session.exec(
        select(WalletTransfer)
        .where((WalletTransfer.sender_id == user_id) | (WalletTransfer.recipient_id == user_id))
        .order_by(WalletTransfer.created_at.desc())
    )
    return list(result.all())


async def get_wallet_summary(session: AsyncSession, user_id: uuid.UUID) -> WalletSummary:
    transfers = await list_my_transfers(session, user_id)
    completed = [transfer for transfer in transfers if transfer.status == TransferStatus.COMPLETED]
    received = sum((transfer.net_amount for transfer in completed if transfer.recipient_id == user_id), Decimal("0.00"))
    sent = sum((transfer.gross_amount for transfer in completed if transfer.sender_id == user_id), Decimal("0.00"))
    fees = sum((transfer.platform_fee for transfer in completed if transfer.sender_id == user_id), Decimal("0.00"))
    roundup = sum((transfer.roundup_amount for transfer in completed if transfer.sender_id == user_id), Decimal("0.00"))
    return WalletSummary(
        received_total=received,
        sent_total=sent,
        fee_total=fees,
        roundup_total=roundup,
        net_flow=received - sent - roundup,
    )
