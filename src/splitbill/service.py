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
from src.common.locking import locked_first
from src.common.sms import send_sms
from src.config import settings
from src.payments import paystack
from src.splitbill.models import (
    SharePayoutStatus,
    ShareStatus,
    SplitBill,
    SplitBillShare,
    SplitBillStatus,
    SplitBillWithdrawal,
    SplitWithdrawalStatus,
    WithdrawalDestination,
)
from src.splitbill.schemas import SplitBillCreate
from src.vaults import service as vault_service
from src.vaults.models import Vault, VaultStatus

logger = logging.getLogger(__name__)

# Older per-share bills: each paid share paid out on its own.
SPLIT_PAYOUT_REFERENCE_PREFIX = "split-payout-"
# Collecting bills: the organizer's one withdrawal to mobile money.
SPLIT_WITHDRAWAL_REFERENCE_PREFIX = "split-wd-"
# Smallest share worth charging - a GHS 0.01 bill split two ways produced a
# GHS 0.00 share, which Paystack can't charge.
MIN_SHARE_GHS = Decimal("1.00")


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
    # No payout number needed up front any more: the money is collected on the
    # bill and the organizer picks mobile money or a vault when withdrawing.
    if len(data.participant_phone_numbers) < 1:
        raise HTTPException(status_code=400, detail="Need at least one participant")

    participants: list[User] = []
    for phone in data.participant_phone_numbers:
        user = await get_user_by_phone(session, phone)
        # Suspended/closed accounts can't log in to pay - same message as an
        # unknown number so this doesn't reveal the account's state.
        if user is None or not user.is_active or user.closed_at is not None:
            raise HTTPException(status_code=404, detail=f"No user found with phone number {phone}")
        if user.id == organizer.id:
            raise HTTPException(status_code=400, detail="Organizer is included automatically, don't list yourself")
        if any(p.id == user.id for p in participants):
            # "0244..." and "+233244..." are the same person - used to get two shares.
            raise HTTPException(status_code=400, detail=f"{phone} is listed more than once")
        participants.append(user)

    # The organizer is part of the split (their share is what they already
    # paid upfront), so the total divides by participants + 1. It used to
    # divide by participants only, overcharging everyone else: GHS 10 across
    # the organizer and 2 friends billed each friend GHS 5 instead of 3.33.
    # The rounding remainder lands on the organizer's own portion.
    amounts = _split_evenly(data.total_amount, len(participants) + 1)
    organizer_amount, participant_amounts = amounts[-1], amounts[:-1]
    if min(participant_amounts) < MIN_SHARE_GHS:
        raise HTTPException(
            status_code=400,
            detail=f"Each share would be under GHS {MIN_SHARE_GHS} - raise the total or split between fewer people",
        )

    bill = SplitBill(organizer_id=organizer.id, title=data.title, total_amount=data.total_amount, collects_funds=True)
    session.add(bill)
    await session.flush()

    # The organizer's own portion: already covered, nothing to collect or pay out.
    session.add(
        SplitBillShare(
            split_bill_id=bill.id,
            user_id=organizer.id,
            gross_amount=organizer_amount,
            platform_fee=Decimal("0.00"),
            net_amount=organizer_amount,
            status=ShareStatus.PAID,
            paid_at=datetime.now(timezone.utc),
            payout_status=SharePayoutStatus.COMPLETED,
        )
    )

    fee_percent = Decimal(settings.PLATFORM_WITHDRAWAL_FEE_PERCENT)
    for amount, participant in zip(participant_amounts, participants):
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


async def get_visible_split_bill(session: AsyncSession, split_bill_id: uuid.UUID, viewer_id: uuid.UUID) -> SplitBill:
    """Only the organizer and the bill's participants may see it - GET used
    to be public, exposing who owes what to anyone holding the id."""
    bill = await get_split_bill(session, split_bill_id)
    if bill.organizer_id != viewer_id:
        mine = await session.exec(
            select(SplitBillShare.id).where(
                SplitBillShare.split_bill_id == bill.id, SplitBillShare.user_id == viewer_id
            )
        )
        if mine.first() is None:
            raise HTTPException(status_code=404, detail="Split bill not found")
    return bill


async def list_my_split_bills(session: AsyncSession, user_id: uuid.UUID) -> list[SplitBill]:
    """Bills the user organized or has a share in, newest first. The app used
    to have no list at all (GET /splits was a 405) and only knew bills whose
    ids it had stored in that browser."""
    shared = select(SplitBillShare.split_bill_id).where(SplitBillShare.user_id == user_id)
    result = await session.exec(
        select(SplitBill)
        .where((SplitBill.organizer_id == user_id) | (SplitBill.id.in_(shared)))
        .order_by(SplitBill.created_at.desc())
    )
    return list(result.all())


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
    """Only while nothing has been paid yet. Once someone has paid, a
    collecting bill is closed instead (close_split_bill) so the organizer can
    withdraw what came in. A payment that lands after cancelling is refunded
    (confirm_share_payment)."""
    if bill.organizer_id != requester_id:
        raise HTTPException(status_code=403, detail="Only the organizer can cancel this split bill")
    if bill.status != SplitBillStatus.OPEN:
        raise HTTPException(status_code=400, detail=f"Cannot cancel a '{bill.status}' split bill")

    shares = await get_shares(session, bill.id)
    # The organizer's own portion is always PAID - only other people's payments block cancelling.
    if any(s.status == ShareStatus.PAID and s.user_id != bill.organizer_id for s in shares):
        raise HTTPException(status_code=400, detail="Cannot cancel a split bill that already has paid shares")

    bill.status = SplitBillStatus.CANCELLED
    session.add(bill)
    # Nobody owes anything now - these used to stay "pending" and keep showing
    # up in participants' "Your pending shares".
    for share in shares:
        if share.status == ShareStatus.PENDING:
            share.status = ShareStatus.CANCELLED
            session.add(share)
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
    await kyc_limits.check_transaction_limit(session, payer, share.gross_amount)

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


def _paid_by_others(bill: SplitBill, shares: list[SplitBillShare]) -> list[SplitBillShare]:
    # The organizer's own portion was never collected - they paid it upfront.
    return [s for s in shares if s.status == ShareStatus.PAID and s.user_id != bill.organizer_id]


def collected_gross(bill: SplitBill, shares: list[SplitBillShare]) -> Decimal:
    """Everything other people paid - what a vault withdrawal receives (no fee)."""
    return sum((s.gross_amount for s in _paid_by_others(bill, shares)), Decimal("0.00"))


def collected_amount(bill: SplitBill, shares: list[SplitBillShare]) -> Decimal:
    """What a mobile money withdrawal receives: the paid shares after the
    platform fee (each share's net_amount, fixed when the bill was created)."""
    return sum((s.net_amount for s in _paid_by_others(bill, shares)), Decimal("0.00"))


def withdrawal_amounts(bill: SplitBill, shares: list[SplitBillShare], destination: WithdrawalDestination) -> tuple[Decimal, Decimal]:
    """(amount the organizer receives, platform fee taken). The fee is waived
    when the money goes into one of their vaults."""
    gross = collected_gross(bill, shares)
    if destination == WithdrawalDestination.VAULT:
        return gross, Decimal("0.00")
    net = collected_amount(bill, shares)
    return net, gross - net


async def _refund_late_payment(session: AsyncSession, share: SplitBillShare, bill: SplitBill) -> None:
    """A payment that landed after its share was cancelled - the organizer
    closed or cancelled the bill while this person's checkout was still open.
    Nothing is owed any more, so the charge goes back. Recorded on the share
    so a repeated webhook or poll can't refund it twice."""
    if share.refund_reference:
        return
    try:
        refund = await paystack.refund_transaction(share.payment_reference)
    except paystack.PaystackError as exc:
        logger.error("Couldn't refund late split-bill payment %s (share %s): %s", share.payment_reference, share.id, exc)
        return
    share.refund_reference = str(refund.get("id") or share.payment_reference)
    session.add(share)
    await session.commit()
    payer = await session.get(User, share.user_id)
    await send_sms(
        payer.phone_number,
        f"'{bill.title}' was closed before your GHS {share.gross_amount} payment came through, "
        f"so we've refunded it. It can take a few days to reach you.",
    )


async def _mark_share_paid(session: AsyncSession, share: SplitBillShare, bill: SplitBill) -> None:
    """Record a successful charge (caller holds the share's row lock and commits)."""
    share.status = ShareStatus.PAID
    share.paid_at = datetime.now(timezone.utc)
    kyc_limits.record_transaction_volume(session, share.user_id, share.gross_amount, "splitbill_share")
    if bill.collects_funds:
        return  # held on the bill until the organizer withdraws
    # Older per-share bills pay the organizer straight away. The participant
    # has paid regardless of what happens to the payout, so a payout error
    # must not turn into a webhook error (Paystack would retry and the payment
    # would stay unrecorded).
    organizer = await session.get(User, bill.organizer_id)
    try:
        await _payout_share(share, bill, organizer)
    except paystack.PaystackError as exc:
        logger.warning("Split-bill payout for share %s failed to start: %s", share.id, exc)
        share.payout_status = SharePayoutStatus.FAILED


async def _settle_if_complete(session: AsyncSession, bill: SplitBill) -> None:
    """OPEN -> SETTLED once nobody owes anything (all shares paid or cancelled).
    For a collecting bill, tell the organizer the money is ready."""
    if bill.status != SplitBillStatus.OPEN:
        return
    shares = await get_shares(session, bill.id)
    if any(s.status == ShareStatus.PENDING for s in shares):
        return
    bill.status = SplitBillStatus.SETTLED
    session.add(bill)
    await session.commit()
    if bill.collects_funds:
        gross = collected_gross(bill, shares)
        if gross > 0:
            organizer = await session.get(User, bill.organizer_id)
            await send_sms(
                organizer.phone_number,
                f"Everyone has paid for '{bill.title}'. GHS {gross} is ready - save it to one of your vaults "
                f"with no fee, or withdraw GHS {collected_amount(bill, shares)} to mobile money. Open Split Bills in the app.",
            )


async def confirm_share_payment(session: AsyncSession, reference: str) -> SplitBillShare:
    """Called from the Paystack webhook handler (and the refresh poll / reconcile
    sweep) once the participant's charge succeeds. Locks the bill, then the
    share - the same order close_split_bill uses - so a payment landing while
    the organizer closes the bill is handled by exactly one of them."""
    share_bill_id = (
        await session.exec(select(SplitBillShare.split_bill_id).where(SplitBillShare.payment_reference == reference))
    ).first()
    if share_bill_id is None:
        raise HTTPException(status_code=404, detail="Share not found")
    bill = await locked_first(session, SplitBill, SplitBill.id == share_bill_id)
    share = await _locked_share(session, SplitBillShare.payment_reference == reference)

    if share.status == ShareStatus.PAID:
        await session.commit()  # release the locks
        return share  # already processed

    verified = await paystack.verify_transaction(reference)
    if verified.get("status") != "success":
        await session.commit()
        return share

    if share.status == ShareStatus.CANCELLED or bill.status == SplitBillStatus.CANCELLED:
        await session.commit()
        await _refund_late_payment(session, share, bill)
        return share

    await _mark_share_paid(session, share, bill)
    session.add(share)
    await session.commit()
    await session.refresh(share)

    await _settle_if_complete(session, bill)
    return share


async def close_split_bill(session: AsyncSession, bill_id: uuid.UUID, requester_id: uuid.UUID) -> SplitBill:
    """Collecting bill where someone isn't going to pay: stop collecting, drop
    what's still owed, and make what was collected withdrawable. Checkouts
    already started are checked with Paystack first, so a payment that has
    gone through is counted rather than cancelled."""
    bill = await locked_first(session, SplitBill, SplitBill.id == bill_id)
    if bill is None:
        raise HTTPException(status_code=404, detail="Split bill not found")
    if bill.organizer_id != requester_id:
        raise HTTPException(status_code=403, detail="Only the organizer can close this split bill")
    if not bill.collects_funds:
        raise HTTPException(status_code=400, detail="This older bill pays each share out as it's paid - nothing to close")
    if bill.status != SplitBillStatus.OPEN:
        raise HTTPException(status_code=400, detail=f"Cannot close a '{bill.status}' split bill")

    shares = list(
        (
            await session.exec(
                select(SplitBillShare)
                .where(SplitBillShare.split_bill_id == bill.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    for share in shares:
        if share.status != ShareStatus.PENDING:
            continue
        paid = False
        if share.payment_reference:
            try:
                paid = (await paystack.verify_transaction(share.payment_reference)).get("status") == "success"
            except paystack.PaystackError:
                paid = False  # checkout never opened / unknown to Paystack
        if paid:
            await _mark_share_paid(session, share, bill)
        else:
            share.status = ShareStatus.CANCELLED
        session.add(share)

    if collected_gross(bill, shares) <= 0:
        # Nothing to withdraw - that's a cancel, and cancel_split_bill's own rules apply.
        await session.rollback()
        raise HTTPException(status_code=400, detail="Nobody has paid yet - cancel the bill instead")

    bill.status = SplitBillStatus.SETTLED
    session.add(bill)
    await session.commit()
    await session.refresh(bill)
    return bill


async def get_withdrawal(session: AsyncSession, bill_id: uuid.UUID) -> SplitBillWithdrawal | None:
    return (
        await session.exec(select(SplitBillWithdrawal).where(SplitBillWithdrawal.split_bill_id == bill_id))
    ).first()


async def _start_momo_withdrawal(withdrawal: SplitBillWithdrawal, bill: SplitBill, organizer: User) -> None:
    """Request the Paystack transfer; leaves the withdrawal PENDING, or FAILED
    with a reason if Paystack refused the request."""
    withdrawal.attempts += 1
    # Unique per attempt: Paystack rejects a reused reference on retry.
    reference = f"{SPLIT_WITHDRAWAL_REFERENCE_PREFIX}{withdrawal.id.hex[:12]}-{withdrawal.attempts}"
    try:
        recipient_code = await paystack.create_transfer_recipient(
            name=organizer.default_account_name or organizer.full_name,
            account_number=organizer.default_momo_number,
            bank_code=organizer.default_momo_bank_code,
        )
        transfer = await paystack.initiate_transfer(
            amount=withdrawal.amount, recipient_code=recipient_code, reason=bill.title, reference=reference
        )
    except paystack.PaystackError as exc:
        logger.warning("Split-bill withdrawal %s couldn't start: %s", withdrawal.id, exc)
        withdrawal.status = SplitWithdrawalStatus.FAILED
        withdrawal.failure_reason = f"Mobile money transfer couldn't start: {exc}"
        return
    withdrawal.payout_reference = transfer.get("reference", reference)
    withdrawal.status = SplitWithdrawalStatus.PENDING
    withdrawal.failure_reason = None
    if transfer.get("status") == "otp":
        logger.warning("Split-bill withdrawal %s is held for OTP in the Paystack dashboard", withdrawal.payout_reference)


async def withdraw(
    session: AsyncSession,
    bill_id: uuid.UUID,
    organizer: User,
    destination: WithdrawalDestination,
    vault_id: uuid.UUID | None,
) -> SplitBillWithdrawal:
    """Take a settled bill's collected money out - to mobile money (a Paystack
    transfer) or into one of the organizer's vaults (instant, no transfer).
    The bill row is locked, and there's one withdrawal per bill, so a double
    tap can't withdraw twice. A failed withdrawal can be retried here, to
    either destination."""
    bill = await locked_first(session, SplitBill, SplitBill.id == bill_id)
    if bill is None:
        raise HTTPException(status_code=404, detail="Split bill not found")
    if bill.organizer_id != organizer.id:
        raise HTTPException(status_code=403, detail="Only the organizer can withdraw from this split bill")
    if not bill.collects_funds:
        raise HTTPException(status_code=400, detail="This older bill paid each share out as it was paid")
    if bill.status == SplitBillStatus.OPEN:
        raise HTTPException(
            status_code=400, detail="Not everyone has paid yet - wait, or close the bill to withdraw what's been collected"
        )
    if bill.status != SplitBillStatus.SETTLED:
        raise HTTPException(status_code=400, detail=f"Cannot withdraw from a '{bill.status}' split bill")

    withdrawal = await locked_first(session, SplitBillWithdrawal, SplitBillWithdrawal.split_bill_id == bill.id)
    if withdrawal is not None and withdrawal.status != SplitWithdrawalStatus.FAILED:
        raise HTTPException(status_code=400, detail="This bill's money has already been withdrawn")

    # Priced by destination: a vault gets everything paid (fee waived),
    # mobile money gets it after the platform fee.
    amount, fee = withdrawal_amounts(bill, await get_shares(session, bill.id), destination)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Nothing was collected on this bill")

    vault = None
    if destination == WithdrawalDestination.VAULT:
        # Locked: credit_roundup adds to the balance, and a vault withdrawal
        # racing it must not work from a stale one.
        vault = await locked_first(session, Vault, Vault.id == vault_id) if vault_id else None
        if vault is None or vault.owner_id != organizer.id:
            raise HTTPException(status_code=404, detail="Vault not found")
        if vault.status in (VaultStatus.WITHDRAWN, VaultStatus.CANCELLED):
            raise HTTPException(status_code=400, detail=f"Can't add money to a '{vault.status}' vault - pick another")
    elif not organizer.default_momo_number or not organizer.default_momo_bank_code:
        raise HTTPException(status_code=400, detail="Add a mobile money payout number in Settings first")

    if withdrawal is None:
        withdrawal = SplitBillWithdrawal(split_bill_id=bill.id, destination=destination, amount=amount)
    # A retry may switch destination (e.g. failed mobile money -> vault), so re-price every time.
    withdrawal.destination = destination
    withdrawal.vault_id = vault.id if vault else None
    withdrawal.amount = amount
    withdrawal.fee = fee
    withdrawal.updated_at = datetime.now(timezone.utc)

    if vault is not None:
        # Same path as a wallet round-up: an already-paid contribution, so it
        # shows on the vault's statement. No Paystack call - the money was
        # collected by the participants' charges.
        withdrawal.attempts += 1
        await vault_service.credit_roundup(session, vault.id, amount, reference=f"split-{bill.id.hex[:12]}")
        withdrawal.status = SplitWithdrawalStatus.COMPLETED
        withdrawal.failure_reason = None
        bill.status = SplitBillStatus.WITHDRAWN
        session.add(bill)
    else:
        await _start_momo_withdrawal(withdrawal, bill, organizer)

    session.add(withdrawal)
    await session.commit()
    await session.refresh(withdrawal)
    if vault is not None:
        await send_sms(
            organizer.phone_number, f"GHS {amount} from '{bill.title}' has been added to your vault '{vault.name}' - no fee."
        )
    return withdrawal


async def handle_withdrawal_payout_event(session: AsyncSession, event: str, reference: str) -> SplitBillWithdrawal | None:
    """Paystack transfer.success / transfer.failed / transfer.reversed for a
    split-bill withdrawal (webhook or reconcile sweep). Locked; a no-op once settled."""
    withdrawal = await locked_first(session, SplitBillWithdrawal, SplitBillWithdrawal.payout_reference == reference)
    if withdrawal is None or withdrawal.status != SplitWithdrawalStatus.PENDING:
        await session.commit()
        return withdrawal

    bill = await locked_first(session, SplitBill, SplitBill.id == withdrawal.split_bill_id)
    organizer = await session.get(User, bill.organizer_id)
    withdrawal.updated_at = datetime.now(timezone.utc)
    if event == "transfer.success":
        withdrawal.status = SplitWithdrawalStatus.COMPLETED
        bill.status = SplitBillStatus.WITHDRAWN
        message = f"GHS {withdrawal.amount} from '{bill.title}' has been sent to your mobile money."
    else:
        logger.warning("Split-bill withdrawal %s: %s", reference, event)
        withdrawal.status = SplitWithdrawalStatus.FAILED
        withdrawal.failure_reason = f"Mobile money transfer {event.split('.')[-1]}"
        message = (f"We couldn't send GHS {withdrawal.amount} from '{bill.title}' to your mobile money. "
                   f"Check your payout number, then withdraw again - to mobile money or a vault.")
    session.add(withdrawal)
    session.add(bill)
    await session.commit()
    await session.refresh(withdrawal)
    await send_sms(organizer.phone_number, message)
    return withdrawal


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
