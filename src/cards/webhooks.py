"""Bitnob virtual-card webhooks (POST /webhooks/bitnob).

Until this existed GlobePay only learned a card had been used when someone
opened its details: purchases, declines and terminations went unrecorded
and nobody was told. Every event is now verified, stored once (card_events),
used to refresh the card from Bitnob, and - where it matters to the
cardholder - sent to them by SMS.

Reference: https://bitnob.dev/api-reference/virtual-cards/webhooks
  - header x-bitnob-signature = HMAC-SHA512(raw body, secret)
  - amounts are micro-units (1 USD = 1,000,000), plus a display amount
  - retried up to 3x on any non-200, with the same event id
Bitnob's pages disagree on key casing (camelCase vs a later snake_case
refresh), so every field is read under both spellings.
"""

import hashlib
import hmac
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.models import User
from src.cards import bitnob_cards, decline_rule, termination_payout
from src.cards.models import CardEvent, CardStatus, VirtualCard
from src.cards.service import sync_card_status
from src.common.sms import send_sms
from src.config import settings

logger = logging.getLogger(__name__)

PREFIX = "virtualcard."

# Money leaving the card. Authorization and settlement arrive for the same
# purchase, so only the first-seen kinds notify the cardholder.
SPEND_NOTIFY = {"transaction.authorization", "transaction.debit", "transaction.contactless", "transaction.crossborder"}
SPEND_QUIET = {"transaction.settlement", "transaction.pre-auth.approved", "transaction.verification"}
MONEY_BACK = {"transaction.credit", "transaction.refund", "transaction.reversed"}
DECLINES = {
    "transaction.declined", "transaction.declined.charge", "transaction.declined.frozen",
    "transaction.declined.terminated", "transaction.authorization.failed",
}
TERMINATED = {"terminated.refund", "transaction.terminated.refund"}


def signature_secret() -> str:
    return settings.BITNOB_WEBHOOK_SECRET or settings.BITNOB_CLIENT_SECRET


def verify_signature(raw_body: bytes, signature: str | None) -> bool:
    secret = signature_secret()
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def _pick(d: dict, *keys: str) -> Any:
    for key in keys:
        if isinstance(d, dict) and d.get(key) not in (None, ""):
            return d[key]
    return None


def _amount_usd(data: dict) -> Decimal | None:
    display = _pick(data, "display_amount", "displayAmount")
    try:
        if display is not None:
            return Decimal(str(display)).quantize(Decimal("0.01"))
        micro = _pick(data, "amount")
        return bitnob_cards.from_micro_units(int(micro)) if micro is not None else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _merchant(data: dict) -> str | None:
    merchant = _pick(data, "merchant", "merchant_name", "merchantName", "description", "narration")
    if isinstance(merchant, dict):
        merchant = _pick(merchant, "name", "merchant_name", "merchantName")
    return str(merchant)[:200] if merchant else None


async def handle_bitnob_webhook(session: AsyncSession, raw_body: bytes, payload: dict) -> str:
    """Returns a short outcome string (logged / returned for debugging).
    Never raises for a well-formed, signed event: Bitnob retries non-200s,
    so problems are logged and the event is still acknowledged."""
    event = str(_pick(payload, "event", "event_type", "eventType", "type") or "")
    data = _pick(payload, "data") or {}
    if not isinstance(data, dict):
        data = {}
    event_id = str(
        _pick(payload, "event_id", "eventId", "id") or hashlib.sha256(raw_body).hexdigest()
    )
    bitnob_card_id = _pick(data, "card_id", "cardId") or _pick(payload, "card_id", "cardId")
    kind = event[len(PREFIX):] if event.startswith(PREFIX) else event

    card = None
    if bitnob_card_id:
        card = (
            await session.exec(select(VirtualCard).where(VirtualCard.bitnob_card_id == str(bitnob_card_id)))
        ).first()

    amount = _amount_usd(data)
    record = CardEvent(
        event_id=event_id,
        event_type=event or "unknown",
        card_id=card.id if card else None,
        bitnob_card_id=str(bitnob_card_id) if bitnob_card_id else None,
        amount_usd=amount,
        reference=_pick(data, "reference", "transaction_reference", "transactionReference"),
        status=_pick(data, "status"),
        reason=_pick(data, "reason", "decline_reason", "declineReason", "message"),
        merchant=_merchant(data),
        payload=payload,
    )
    session.add(record)
    try:
        await session.commit()
    except IntegrityError:
        # Same event_id already stored: a Bitnob retry. Acknowledge, do nothing.
        await session.rollback()
        return "duplicate"

    if card is None:
        logger.info("Bitnob card webhook %s for unknown card %s - stored only", event, bitnob_card_id)
        return "unknown_card"

    owner = await session.get(User, card.user_id)
    last4 = (card.masked_pan or "")[-4:] or "card"
    amount_text = f"USD {amount}" if amount is not None else "a payment"
    at = f" at {record.merchant}" if record.merchant else ""

    try:
        if kind == "terminated.refund":
            # The card's leftover balance, returned to the company wallet - pay it
            # to the owner (no-op if the in-app terminate already started it).
            # start_termination_payout also sends the owner their SMS.
            await termination_payout.start_termination_payout(session, card.id, amount, source="webhook")
        elif kind in TERMINATED:
            # Per-transaction cleanup refunds during termination: stored only. The
            # card-level terminated.refund above is what gets paid out, so paying
            # these too could pay twice.
            card.status = CardStatus.TERMINATED
            session.add(card)
            await session.commit()
        else:
            # Balance/status straight from Bitnob rather than doing arithmetic on
            # events that can arrive out of order.
            await sync_card_status(session, card)
            if kind in SPEND_NOTIFY:
                # Warn now if the same charge (e.g. a subscription renewal) couldn't be paid again.
                await send_sms(owner.phone_number, f"GlobePay card ending {last4}: {amount_text}{at}. "
                               f"Balance USD {card.balance}.{decline_rule.renewal_warning(card, amount)}")
            elif kind in MONEY_BACK:
                await send_sms(owner.phone_number, f"GlobePay card ending {last4}: {amount_text} returned to your card.")
            elif kind == "transaction.declined.charge":
                # Bitnob charged a decline-rule fee; its violation count is authoritative.
                before = card.decline_strikes
                decline_rule.record_charge(card, data)
                session.add(card)
                await session.commit()
                if card.decline_strikes > before:  # a strike we hadn't seen as a decline
                    await send_sms(owner.phone_number, decline_rule.strike_message(card, "a payment", ""))
            elif kind in DECLINES and decline_rule.is_violation(kind, record.reason):
                # Counts toward Bitnob's 3-strike rule - say exactly where the card stands.
                decline_rule.record_violation(card)
                session.add(card)
                await session.commit()
                await send_sms(owner.phone_number, decline_rule.strike_message(card, amount_text, at))
            elif kind in DECLINES and kind != "transaction.declined.terminated":
                reason = f" ({record.reason})" if record.reason else ""
                await send_sms(
                    owner.phone_number,
                    f"GlobePay card ending {last4}: a {amount_text} payment{at} was declined{reason}. "
                    f"Check the card number, expiry, CVV and billing address you entered.",
                )
            elif kind == "created.failed" and card.status == CardStatus.PROVISIONING:
                card.status = CardStatus.DELIVERY_FAILED
                card.failure_reason = f"Bitnob card creation failed: {record.reason or 'no reason given'}"
                session.add(card)
                await session.commit()
            elif kind == "expiration":
                # Card state was re-read from Bitnob above. Subscriptions on it will
                # now decline - tell the owner before those count as strikes.
                await send_sms(
                    owner.phone_number,
                    f"Your GlobePay card ending {last4} has expired and can't be used for payments. "
                    f"Move any subscriptions on it to another card.",
                )
            elif kind not in SPEND_QUIET and kind not in {"created.completed", "regularized"}:
                logger.info("Bitnob card webhook %s stored without further action", event)
    except Exception:
        # The event is stored; don't make Bitnob retry and double-notify.
        logger.exception("Processing Bitnob card webhook %s (%s) failed after storing it", event_id, event)
        await session.rollback()
        return "stored_with_error"
    return kind or "stored"
