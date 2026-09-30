"""Paying a terminated card's leftover balance back to its owner.

When a card is terminated Bitnob sends whatever was left on it back to the
company wallet (terminate response: remaining_balance + balance_returned;
webhook: virtualcard.terminated.refund) - never to the cardholder. Lite cards
can't be withdrawn from first (confirmed live 2026-09-29: "withdrawals are not
supported for lite cards"), so GlobePay pays the owner the GHS equivalent to
their mobile money, funded by the money Bitnob just returned.

A termination can be noticed three ways - the owner terminating in the app,
the Bitnob webhook, or a status sync finding the card terminated (e.g. Bitnob
closed it after repeated declines). Whichever gets there first starts the
payout; the row lock + NOT_STARTED check make the others no-ops.
"""

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.models import User
from src.cards import momo_payout
from src.cards.models import CardStatus, TerminationPayoutStatus, VirtualCard
from src.common.locking import locked_first
from src.common.sms import send_sms
from src.payments import paystack

logger = logging.getLogger(__name__)

PAYOUT_REFERENCE_PREFIX = "card-term-"
# Below this a Paystack transfer isn't worth attempting (and may be refused).
MIN_PAYOUT_GHS = Decimal("1.00")


usd_to_ghs = momo_payout.usd_to_ghs


def _last4(card: VirtualCard) -> str:
    return (card.masked_pan or "")[-4:] or "card"


async def _send(card: VirtualCard, owner: User) -> None:
    """Request the transfer and move the card to payout PENDING. Raises
    PaystackError if there's no destination or Paystack refuses it."""
    # Unique per attempt: Paystack rejects a reused reference on retry.
    reference = f"{PAYOUT_REFERENCE_PREFIX}{card.id}-{uuid.uuid4().hex[:8]}"
    card.termination_payout_reference = await momo_payout.send(
        owner, card.termination_payout_ghs, f"Balance of terminated GlobePay card {_last4(card)}", reference
    )
    card.termination_payout_status = TerminationPayoutStatus.PENDING


async def start_termination_payout(
    session: AsyncSession, card_id: uuid.UUID, refund_usd: Decimal | None, source: str
) -> VirtualCard | None:
    """Record what Bitnob returned for a terminated card and pay it to the
    owner. Idempotent: only the first caller for a card does anything."""
    card = await locked_first(session, VirtualCard, VirtualCard.id == card_id)
    if card is None or card.termination_payout_status != TerminationPayoutStatus.NOT_STARTED:
        # Release the lock with a commit (nothing is pending): a rollback would
        # expire `card`, and the caller reading it afterwards raises MissingGreenlet.
        await session.commit()
        return card

    owner = await session.get(User, card.user_id)
    refund_usd = max(refund_usd or Decimal("0"), Decimal("0")).quantize(Decimal("0.01"))
    card.status = CardStatus.TERMINATED
    card.balance = Decimal("0.00")  # the money has left the card (to us, then to the owner)
    card.termination_refund_usd = refund_usd
    # Bitnob's decline-rule fees were charged to the company wallet; like every
    # Bitnob fee they're passed on - taken from what's paid back (decline_rule.py).
    fees_usd = min(card.decline_fees_usd or Decimal("0"), refund_usd)
    card.termination_payout_ghs = usd_to_ghs(refund_usd - fees_usd)
    card.updated_at = datetime.now(timezone.utc)
    last4 = _last4(card)
    fee_note = f" (after USD {fees_usd} in declined-payment fees)" if fees_usd else ""

    if card.termination_payout_ghs < MIN_PAYOUT_GHS:
        card.termination_payout_status = TerminationPayoutStatus.NOT_NEEDED
        message = f"Your GlobePay card ending {last4} has been terminated."
    else:
        try:
            await _send(card, owner)
            message = (f"Your GlobePay card ending {last4} has been terminated. Its remaining balance, "
                       f"GHS {card.termination_payout_ghs}{fee_note}, is on its way to your mobile money.")
        except paystack.PaystackError as exc:
            logger.warning("Termination payout for card %s failed to start: %s", card.id, exc)
            card.termination_payout_status = TerminationPayoutStatus.FAILED
            message = (f"Your GlobePay card ending {last4} has been terminated. We couldn't send its "
                       f"remaining GHS {card.termination_payout_ghs} to you yet - check your payout "
                       f"number in Settings, then tap Retry payout on the card.")

    logger.info("Card %s terminated (%s): USD %s -> GHS %s, payout %s", card.id, source, refund_usd,
                card.termination_payout_ghs, card.termination_payout_status)
    phone = owner.phone_number  # read before commit, which may expire loaded rows
    session.add(card)
    await session.commit()
    await session.refresh(card)
    await send_sms(phone, message)
    return card


async def retry_termination_payout(session: AsyncSession, card_id: uuid.UUID, owner_id: uuid.UUID) -> VirtualCard:
    """Owner re-sends a payout that failed (e.g. after setting a payout number)."""
    card = await locked_first(session, VirtualCard, VirtualCard.id == card_id, VirtualCard.user_id == owner_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    if card.termination_payout_status != TerminationPayoutStatus.FAILED:
        raise HTTPException(status_code=400, detail="Only a failed balance payout can be retried")
    owner = await session.get(User, owner_id)
    try:
        await _send(card, owner)
    except paystack.PaystackError as exc:
        await session.rollback()
        raise HTTPException(status_code=502, detail=f"Payout could not be started: {exc}") from exc
    card.updated_at = datetime.now(timezone.utc)
    session.add(card)
    await session.commit()
    await session.refresh(card)
    return card


async def handle_payout_event(session: AsyncSession, event: str, reference: str) -> VirtualCard | None:
    """Paystack transfer.success / transfer.failed / transfer.reversed for a
    termination payout (webhook or reconcile sweep)."""
    card = await locked_first(session, VirtualCard, VirtualCard.termination_payout_reference == reference)
    if card is None or card.termination_payout_status != TerminationPayoutStatus.PENDING:
        await session.commit()  # release the lock without expiring `card` (see start_termination_payout)
        return card  # unknown/stale reference or already settled (duplicate delivery)

    owner = await session.get(User, card.user_id)
    if event == "transfer.success":
        card.termination_payout_status = TerminationPayoutStatus.COMPLETED
        message = (f"GHS {card.termination_payout_ghs} from your terminated GlobePay card ending "
                   f"{_last4(card)} has been sent to your mobile money.")
    else:
        logger.warning("Card termination payout %s for card %s: %s", reference, card.id, event)
        card.termination_payout_status = TerminationPayoutStatus.FAILED
        message = (f"We couldn't deliver GHS {card.termination_payout_ghs} from your terminated card ending "
                   f"{_last4(card)}. Check your payout number in Settings, then tap Retry payout on the card.")
    card.updated_at = datetime.now(timezone.utc)
    phone = owner.phone_number  # read before commit, which may expire loaded rows
    session.add(card)
    await session.commit()
    await session.refresh(card)
    await send_sms(phone, message)
    return card
