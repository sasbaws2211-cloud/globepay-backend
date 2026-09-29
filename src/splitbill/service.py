import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.models import User
from src.auth.service import get_user_by_phone
from src.common.kyc_limits import check_transaction_limit, record_transaction_volume
from src.common.sms import send_sms
from src.config import settings
from src.payments import paystack
from src.splitbill.models import SharePayoutStatus, ShareStatus, SplitBill, SplitBillShare, SplitBillStatus
from src.splitbill.schemas import SplitBillCreate

logger = logging.getLogger(__name__)

SPLIT_PAYOUT_REFERENCE_PREFIX = "split-payout-"


def _split_evenly(total: Decimal, count: int) -> list[Decimal]:
    """Divide `total` into `count` shares that sum back to exactly `total`,
    putting any rounding remainder on the last share rather than losing or
    inventing pesewas."""
    base = (total / count).quantize(Decimal("0.01"))
    shares = [base] * count
    remainder = total - (base * count)
    shares[-1] += remainder
    return shares


async def create_split_bill(session: AsyncSession, organizer: User, data: SplitBillCreate) -> SplitBill:
    if not organizer.default_momo_number or not organizer.default_momo_bank_code:
        raise HTTPException(
            status_code=400,
            detail="Set a payout destination (PUT /auth/me/payout-destination) before creating a split bill",
        )
    if len(data.participant_phone_numbers) < 1:
        raise HTTPException(status_code=400, detail="Need at least one participant")

    participants = []
    for phone in data.participant_phone_numbers:
        user = await get_user_by_phone(session, phone)
        if user is None:
            raise HTTPException(status_code=404, detail=f"No user found with phone number {phone}")
        if user.id == organizer.id:
            raise HTTPException(status_code=400, detail="Organizer is included automatically, don't list yourself")
        participants.append(user)

    bill = SplitBill(organizer_id=organizer.id, title=data.title, total_amount=data.total_amount)
    session.add(bill)
    await session.flush()

    fee_percent = Decimal(settings.PLATFORM_WITHDRAWAL_FEE_PERCENT)
    for amount, participant in zip(_split_evenly(data.total_amount, len(participants)), participants):
        fee = (amount * fee_percent / Decimal(100)).quantize(Decimal("0.01"))
        session.add(
            SplitBillShare(
                split_bill_id=bill.id,
                user_id=participant.id,
                gross_amount=amount,
                platform_fee=fee,
                net_amount=amount - fee,
            )
        )

    await session.commit()
    await session.refresh(bill)
    return bill


async def get_split_bill(session: AsyncSession, split_bill_id: uuid.UUID) -> SplitBill:
    bill = await session.get(SplitBill, split_bill_id)
    if bill is None:
        raise HTTPException(status_code=404, detail="Split bill not found")
    return bill


async def get_shares(session: AsyncSession, split_bill_id: uuid.UUID) -> list[SplitBillShare]:
    result = await session.exec(select(SplitBillShare).where(SplitBillShare.split_bill_id == split_bill_id))
    return list(result.all())


async def get_my_share(session: AsyncSession, split_bill_id: uuid.UUID, user_id: uuid.UUID) -> SplitBillShare:
    result = await session.exec(
        select(SplitBillShare).where(
            SplitBillShare.split_bill_id == split_bill_id, SplitBillShare.user_id == user_id
        )
    )
    share = result.first()
    if share is None:
        raise HTTPException(status_code=404, detail="You are not a participant in this split bill")
    return share


async def cancel_split_bill(session: AsyncSession, bill: SplitBill, requester_id: uuid.UUID) -> SplitBill:
    """Only while nothing has been paid yet - once a share is PAID, that
    money already moved to the organizer via a real transfer, and refunding
    it back out is a different workflow this doesn't attempt to solve."""
    if bill.organizer_id != requester_id:
        raise HTTPException(status_code=403, detail="Only the organizer can cancel this split bill")
    if bill.status != SplitBillStatus.OPEN:
        raise HTTPException(status_code=400, detail=f"Cannot cancel a '{bill.status}' split bill")

    shares = await get_shares(session, bill.id)
    if any(s.status == ShareStatus.PAID for s in shares):
        raise HTTPException(status_code=400, detail="Cannot cancel a split bill that already has paid shares")

    bill.status = SplitBillStatus.CANCELLED
    session.add(bill)
    await session.commit()
    await session.refresh(bill)
    return bill


async def initiate_share_payment(session: AsyncSession, share: SplitBillShare, email: str) -> dict:
    if share.status == ShareStatus.PAID:
        raise HTTPException(status_code=400, detail="Already paid")

    bill = await get_split_bill(session, share.split_bill_id)
    if bill.status != SplitBillStatus.OPEN:
        raise HTTPException(status_code=400, detail=f"Cannot pay a share on a '{bill.status}' split bill")

    payer = await session.get(User, share.user_id)
    await check_transaction_limit(session, payer, share.gross_amount)

    reference = f"split-{share.id}-{uuid.uuid4().hex[:8]}"
    share.payment_reference = reference
    session.add(share)
    await session.commit()

    data = await paystack.initialize_transaction(
        email=email,
        amount=share.gross_amount,
        reference=reference,
        metadata={"type": "splitbill_share", "share_id": str(share.id)},
    )
    return {"authorization_url": data["authorization_url"], "reference": reference}


async def _locked_share(session: AsyncSession, *where) -> SplitBillShare | None:
    """SELECT ... FOR UPDATE so a duplicate webhook delivery or a double-tapped
    retry waits for the first to commit; populate_existing forces a re-read
    even if the row is already in the session's identity map."""
    result = await session.exec(
        select(SplitBillShare).where(*where).with_for_update().execution_options(populate_existing=True)
    )
    return result.first()


async def _payout_share(share: SplitBillShare, bill: SplitBill, organizer: User) -> None:
    """Request the organizer's payout and move the share to payout PENDING.
    Raises PaystackError if the request itself is rejected."""
    if not organizer.default_momo_number or not organizer.default_momo_bank_code:
        raise paystack.PaystackError("Organizer has no payout destination set")
    recipient_code = await paystack.create_transfer_recipient(
        name=organizer.default_account_name or organizer.full_name,
        account_number=organizer.default_momo_number,
        bank_code=organizer.default_momo_bank_code,
    )
    # Unique per attempt: Paystack rejects a reused reference on retry.
    reference = f"{SPLIT_PAYOUT_REFERENCE_PREFIX}{share.id}-{uuid.uuid4().hex[:8]}"
    transfer = await paystack.initiate_transfer(
        amount=share.net_amount, recipient_code=recipient_code, reason=bill.title, reference=reference
    )
    share.payout_reference = transfer.get("reference", reference)
    share.payout_status = SharePayoutStatus.PENDING
    if transfer.get("status") == "otp":
        logger.warning(
            "Split-bill payout %s is held for OTP - Transfer OTP is enabled on the Paystack account, "
            "so it won't be sent until finalized in the Paystack dashboard.",
            share.payout_reference,
        )


async def confirm_share_payment(session: AsyncSession, reference: str) -> SplitBillShare:
    """Called from the Paystack webhook handler once the participant's charge succeeds."""
    share = await _locked_share(session, SplitBillShare.payment_reference == reference)
    if share is None:
        raise HTTPException(status_code=404, detail="Share not found")

    if share.status == ShareStatus.PAID:
        return share  # already processed

    verified = await paystack.verify_transaction(reference)
    if verified.get("status") != "success":
        return share

    bill = await get_split_bill(session, share.split_bill_id)
    organizer = await session.get(User, bill.organizer_id)

    # The participant has paid regardless of what happens to the payout, so
    # record that first - a payout error must not turn into a webhook error
    # (Paystack would retry and the payment would stay unrecorded).
    share.status = ShareStatus.PAID
    share.paid_at = datetime.now(timezone.utc)
    record_transaction_volume(session, share.user_id, share.gross_amount, "splitbill_share")

    try:
        await _payout_share(share, bill, organizer)
    except paystack.PaystackError as exc:
        logger.warning("Split-bill payout for share %s failed to start: %s", share.id, exc)
        share.payout_status = SharePayoutStatus.FAILED

    session.add(share)
    await session.commit()
    await session.refresh(share)

    all_shares = await get_shares(session, bill.id)
    if all(s.status == ShareStatus.PAID for s in all_shares):
        bill.status = SplitBillStatus.SETTLED
        session.add(bill)
        await session.commit()

    return share


async def retry_share_payout(
    session: AsyncSession, bill: SplitBill, share_id: uuid.UUID, requester_id: uuid.UUID
) -> SplitBillShare:
    """Organizer re-sends a payout that failed (e.g. after fixing their payout destination)."""
    if bill.organizer_id != requester_id:
        raise HTTPException(status_code=403, detail="Only the organizer can retry a payout")
    share = await _locked_share(session, SplitBillShare.id == share_id, SplitBillShare.split_bill_id == bill.id)
    if share is None:
        raise HTTPException(status_code=404, detail="Share not found")
    if share.status != ShareStatus.PAID or share.payout_status != SharePayoutStatus.FAILED:
        raise HTTPException(status_code=400, detail="Only a paid share whose payout failed can be retried")

    organizer = await session.get(User, bill.organizer_id)
    try:
        await _payout_share(share, bill, organizer)
    except paystack.PaystackError as exc:
        raise HTTPException(status_code=502, detail=f"Payout could not be started: {exc}") from exc

    session.add(share)
    await session.commit()
    await session.refresh(share)
    return share


async def handle_payout_event(session: AsyncSession, event: str, reference: str) -> SplitBillShare | None:
    """Paystack transfer.success / transfer.failed / transfer.reversed webhook
    for an organizer payout."""
    share = await _locked_share(session, SplitBillShare.payout_reference == reference)
    if share is None or share.payout_status != SharePayoutStatus.PENDING:
        return share  # unknown/stale reference or already processed (duplicate delivery)

    bill = await get_split_bill(session, share.split_bill_id)
    organizer = await session.get(User, bill.organizer_id)
    if event == "transfer.success":
        share.payout_status = SharePayoutStatus.COMPLETED
    else:
        logger.warning("Split-bill payout %s for share %s: %s", reference, share.id, event)
        share.payout_status = SharePayoutStatus.FAILED
        await send_sms(
            organizer.phone_number,
            f"We couldn't deliver GHS {share.net_amount} from '{bill.title}' to your mobile money wallet. "
            f"Check your payout details in the app and retry the payout.",
        )

    session.add(share)
    await session.commit()
    await session.refresh(share)
    return share


async def list_my_pending_shares(session: AsyncSession, user_id: uuid.UUID) -> list[SplitBillShare]:
    result = await session.exec(
        select(SplitBillShare).where(SplitBillShare.user_id == user_id, SplitBillShare.status == ShareStatus.PENDING)
    )
    return list(result.all())
