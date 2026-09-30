import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.models import User
from src.cards import bitnob_cards, termination_payout
from src.common import bitnob_balance, kyc_limits
from src.cards.models import CardEvent, CardStatus, TerminationPayoutStatus, VirtualCard
from src.cards.schemas import CardCreate, CardTerminate, CardTransactionRead
from src.common.locking import locked_first
from src.common.sms import send_sms
from src.config import settings
from src.payments import paystack, refunds

logger = logging.getLogger(__name__)

MIN_CARD_USD = Decimal("2.00")  # confirmed live: Bitnob rejects card creation below $2
# Lite cards (the only type GlobePay issues): loaded once at creation, up to
# this cap, never topped up.
LITE_MAX_LOAD_USD = Decimal(bitnob_cards.LITE_MAX_LOAD_USD)
MAX_DELIVERY_RETRIES = 3


def _ghs_to_usd(amount_ghs: Decimal) -> Decimal:
    return (amount_ghs / Decimal(str(settings.DEMO_GHS_USD_RATE))).quantize(Decimal("0.01"))


def _usd_to_ghs(amount_usd: Decimal) -> Decimal:
    return (amount_usd * Decimal(str(settings.DEMO_GHS_USD_RATE))).quantize(Decimal("0.01"))


def _bitnob_fees_usd(amount_usd: Decimal) -> Decimal:
    """Bitnob's fees on a new card, debited from the company float and passed
    on to the user (confirmed live 2026-09-29/30 against the balance and the
    card's transaction list): the funding fee on its one load - $1 under
    $100, else 1% - plus $1 for creating the card."""
    if amount_usd < bitnob_cards.FUNDING_FEE_FLAT_BELOW_USD:
        funding_fee = Decimal(bitnob_cards.FUNDING_FEE_FLAT_USD)
    else:
        funding_fee = (amount_usd * bitnob_cards.FUNDING_FEE_PERCENT / 100).quantize(Decimal("0.01"))
    return funding_fee + Decimal(bitnob_cards.CARD_CREATION_FEE_USD)


def price_load(amount_ghs: Decimal) -> dict:
    """Validate a new card's one-time load against Bitnob's lite-card limits
    and price it: what goes on the card, the passed-on fees, and the total
    charged. Raises HTTPException(400) with a user-facing reason."""
    amount_usd = _ghs_to_usd(amount_ghs)
    min_ghs = _usd_to_ghs(MIN_CARD_USD) + Decimal("0.01")  # rounded up so it never converts to under $2
    if amount_usd < MIN_CARD_USD:
        raise HTTPException(status_code=400, detail=f"The minimum is GHS {min_ghs} (${MIN_CARD_USD})")
    if amount_usd > LITE_MAX_LOAD_USD:
        raise HTTPException(
            status_code=400,
            detail=f"A card can hold at most GHS {_usd_to_ghs(LITE_MAX_LOAD_USD)} (${LITE_MAX_LOAD_USD}), "
            f"loaded once when it's created.",
        )
    fee_usd = _bitnob_fees_usd(amount_usd)
    fee_ghs = _usd_to_ghs(fee_usd)
    return {
        "amount_ghs": amount_ghs,
        "amount_usd": amount_usd,
        "fee_ghs": fee_ghs,
        "fee_usd": fee_usd,
        "total_ghs": amount_ghs + fee_ghs,
    }


async def _ensure_float_covers(load_usd: Decimal, fee_usd: Decimal) -> None:
    """Refuse BEFORE payment if our Bitnob float can't fund this load plus
    Bitnob's fees - otherwise card creation fails after the user paid.
    An unknown balance (Bitnob's post-debit 0, see common/bitnob_balance)
    doesn't block; the paid-but-undelivered path (retry/refund) covers it."""
    try:
        available = await bitnob_balance.available_balance("USDC")
    except bitnob_cards.BitnobError:
        available = None
    if available is not None and load_usd + fee_usd > available:
        logger.warning("Bitnob float %s can't cover a card load of %s + fees %s", available, load_usd, fee_usd)
        raise HTTPException(status_code=503, detail="Cards are temporarily unavailable - please try again later")


def get_card_limits() -> dict:
    """What the app shows before someone pays - single source of truth so the
    UI can't drift from what the backend (and Bitnob) will accept."""
    rate = Decimal(str(settings.DEMO_GHS_USD_RATE))
    return {
        "card_type": "lite",
        "can_top_up": False,
        # Round the GHS minimum up so the converted USD never lands a pesewa under $2.
        "min_load_ghs": _usd_to_ghs(MIN_CARD_USD) + Decimal("0.01"),
        "max_load_ghs": _usd_to_ghs(LITE_MAX_LOAD_USD),
        "min_load_usd": MIN_CARD_USD,
        "max_load_usd": LITE_MAX_LOAD_USD,
        "max_cards_per_phone": bitnob_cards.MAX_LITE_CARDS_PER_CUSTOMER,
        "creation_fee_usd": Decimal(bitnob_cards.CARD_CREATION_FEE_USD),
        "ghs_per_usd": rate,
        "fees": {
            "creation_usd": Decimal(bitnob_cards.CARD_CREATION_FEE_USD),
            "funding_flat_usd": Decimal(bitnob_cards.FUNDING_FEE_FLAT_USD),
            "funding_flat_below_usd": Decimal(bitnob_cards.FUNDING_FEE_FLAT_BELOW_USD),
            "funding_percent": Decimal(bitnob_cards.FUNDING_FEE_PERCENT),
        },
    }


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(maxsplit=1)
    return (parts[0], parts[1]) if len(parts) > 1 else (parts[0], parts[0])


def _normalize_local_phone(dial_code: str, local_phone_number: str) -> str:
    """Bitnob identifies a lite-card customer by phone number, so the same
    person entered as "0200000001", "200000001" or "+233200000001" must map
    to one value - otherwise the per-customer card cap check misses and
    Bitnob may split one person into several customers."""
    digits = "".join(ch for ch in local_phone_number if ch.isdigit())
    dial_digits = "".join(ch for ch in dial_code if ch.isdigit())
    if dial_digits and digits.startswith(dial_digits) and len(digits) > len(dial_digits) + 6:
        digits = digits[len(dial_digits):]
    return digits.lstrip("0")


async def _ensure_card_slot_available(local_phone_number: str) -> None:
    """Check Bitnob's per-customer lite-card cap BEFORE taking payment -
    previously this was only discovered when card creation failed after the
    charge ("customer already has 3 active cards, maximum is 3"). Fails
    closed if Bitnob can't be reached: a refused request is recoverable, a
    charge that can't be delivered needs a refund."""
    try:
        customer = await bitnob_cards.find_customer_by_phone(local_phone_number)
    except bitnob_cards.BitnobError as e:
        raise HTTPException(
            status_code=503, detail="Couldn't check your card allowance right now - please try again shortly"
        ) from e
    if customer is None:
        return  # new to Bitnob: the card request will create the customer
    counts = customer.get("card_counts") or {}
    live_cards = sum(int(counts.get(k) or 0) for k in ("active", "frozen", "pending"))
    if live_cards >= bitnob_cards.MAX_LITE_CARDS_PER_CUSTOMER:
        raise HTTPException(
            status_code=400,
            detail=f"You already have {live_cards} cards on this phone number - the maximum is "
            f"{bitnob_cards.MAX_LITE_CARDS_PER_CUSTOMER}. Terminate a card you no longer use to create a new one.",
        )


async def initiate_card_creation(session: AsyncSession, user: User, data: CardCreate) -> dict:
    priced = price_load(data.initial_funding_ghs)
    await kyc_limits.check_transaction_limit(session, user, priced["total_ghs"])

    local_phone = _normalize_local_phone(data.dial_code, data.local_phone_number)
    if not local_phone:
        raise HTTPException(status_code=400, detail="Enter the card phone number")
    await _ensure_card_slot_available(local_phone)
    await _ensure_float_covers(priced["amount_usd"], priced["fee_usd"])

    reference = f"card-{uuid.uuid4().hex[:14]}"
    card = VirtualCard(
        user_id=user.id,
        initial_funding_ghs=priced["amount_ghs"],
        fee_ghs=priced["fee_ghs"],
        payment_reference=reference,
        dial_code=data.dial_code,
        local_phone_number=local_phone,
    )
    session.add(card)
    await session.commit()

    checkout = await paystack.initialize_transaction(
        email=data.sender_email,
        amount=card.charged_ghs,
        reference=reference,
        metadata={"type": "card_creation", "card_id": str(card.id)},
    )
    return {"authorization_url": checkout["authorization_url"], "reference": reference}


async def _attempt_card_creation(card: VirtualCard, user: User) -> None:
    """Mutates card.status/bitnob_*/failure_reason in place. Never raises:
    the GHS payment has already been collected by the time this runs (both
    on first attempt and on retry), so any failure here must land in
    DELIVERY_FAILED - a retryable/refundable state - not bubble up."""
    first_name, last_name = _split_name(user.full_name)
    amount_usd = _ghs_to_usd(card.initial_funding_ghs)

    try:
        result_data = await bitnob_cards.create_lite_card(
            first_name=first_name,
            last_name=last_name,
            email=user.email or f"{user.phone_number}@example.com",
            phone_number=card.local_phone_number,
            dial_code=card.dial_code,
            amount=amount_usd,
        )
        bitnob_balance.record_debit("USDC", amount_usd + _bitnob_fees_usd(amount_usd))
        card_data = result_data.get("data", {}).get("card", {})
        card.bitnob_card_id = card_data.get("id")
        card.bitnob_customer_id = card_data.get("customer_id")
        card.card_brand = card_data.get("card_brand")
        card.masked_pan = card_data.get("masked_pan")
        card.status = CardStatus.PROVISIONING
    except bitnob_cards.BitnobError as e:
        # GHS payment already succeeded but Bitnob card creation then
        # failed - "paid but not delivered". DELIVERY_FAILED lets the user
        # retry (retry_card_creation) or get refunded (refund_card_creation)
        # instead of the money just vanishing.
        card.status = CardStatus.DELIVERY_FAILED
        card.failure_reason = f"Bitnob sandbox call failed: {e.user_message()}"

    card.updated_at = datetime.now(timezone.utc)


async def confirm_card_payment(session: AsyncSession, reference: str) -> VirtualCard:
    """Called from the Paystack webhook handler once the GHS payment succeeds,
    and from the refresh/reconcile pollers - locked so the card can't be created twice."""
    card = await locked_first(session, VirtualCard, VirtualCard.payment_reference == reference)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")

    if card.status != CardStatus.PENDING_PAYMENT:
        return card  # already processed

    verified = await paystack.verify_transaction(reference)
    if verified.get("status") != "success":
        card.status = CardStatus.FAILED
        card.failure_reason = "GHS payment not successful"
        session.add(card)
        await session.commit()
        return card

    user = await session.get(User, card.user_id)
    kyc_limits.record_transaction_volume(session, user.id, card.charged_ghs, "card_creation")
    await _attempt_card_creation(card, user)

    session.add(card)
    await session.commit()
    await session.refresh(card)
    return card


async def retry_card_creation(session: AsyncSession, card: VirtualCard) -> VirtualCard:
    if card.status != CardStatus.DELIVERY_FAILED:
        raise HTTPException(
            status_code=400, detail="Only a card stuck after payment (delivery_failed) can be retried"
        )
    if card.retry_count >= MAX_DELIVERY_RETRIES:
        raise HTTPException(
            status_code=400,
            detail=f"Retry limit reached ({MAX_DELIVERY_RETRIES}) - request a refund instead",
        )
    # If the phone number is still at Bitnob's card cap, a retry fails the
    # same way - say so instead of burning one of the retry attempts.
    try:
        await _ensure_card_slot_available(_normalize_local_phone(card.dial_code, card.local_phone_number))
    except HTTPException as e:
        if e.status_code == 400:
            raise HTTPException(status_code=400, detail=f"{e.detail} Or request a refund for this card.") from e
        raise

    card.retry_count += 1
    user = await session.get(User, card.user_id)
    await _attempt_card_creation(card, user)
    session.add(card)
    await session.commit()
    await session.refresh(card)

    if card.status == CardStatus.PROVISIONING:
        await send_sms(user.phone_number, "Good news - your virtual card was created on retry.")
    return card


async def refund_card_creation(session: AsyncSession, card: VirtualCard) -> VirtualCard:
    """Moves the card to REFUND_PENDING; REFUNDED only once Paystack reports
    the refund processed - see src/payments/refunds.py."""
    return await refunds.start_refund(session, refunds.CARD_CREATION, card.id)


async def sync_card_status(session: AsyncSession, card: VirtualCard) -> VirtualCard:
    """Lite card creation returns 'pending' and settles to 'active'
    asynchronously (confirmed ~1s in sandbox testing) - call this to
    refresh status/balance/masked_pan from Bitnob's real state."""
    if card.bitnob_card_id is None:
        return card

    try:
        result = await bitnob_cards.get_card(card.bitnob_card_id)
        card_data = result.get("data", {}).get("card", {})
        bitnob_status = card_data.get("status", "")
        balance_before = card.balance
        card.masked_pan = card_data.get("masked_pan", card.masked_pan)
        card.card_brand = card_data.get("card_brand", card.card_brand)
        card.balance = bitnob_cards.from_micro_units(int(card_data.get("balance_amount", 0)))

        if bitnob_status == "active":
            card.status = CardStatus.ACTIVE
        elif bitnob_status == "frozen":
            card.status = CardStatus.FROZEN
        elif bitnob_status == "terminated":
            card.status = CardStatus.TERMINATED

        card.updated_at = datetime.now(timezone.utc)
        session.add(card)
        await session.commit()
        await session.refresh(card)

        # Terminated outside the app (Bitnob's decline rule, the dashboard) and
        # the webhook hasn't told us: pay out the last balance we saw. Balance
        # after termination reads 0, hence balance_before.
        if bitnob_status == "terminated" and card.termination_payout_status == TerminationPayoutStatus.NOT_STARTED:
            card = await termination_payout.start_termination_payout(
                session, card.id, balance_before, source="status sync"
            ) or card
    except bitnob_cards.BitnobError:
        pass  # keep last-known state rather than fail the read

    return card


async def get_card(session: AsyncSession, card_id: uuid.UUID, owner_id: uuid.UUID) -> VirtualCard:
    card = await session.get(VirtualCard, card_id)
    if card is None or card.user_id != owner_id:
        raise HTTPException(status_code=404, detail="Card not found")
    return card


async def list_my_cards(session: AsyncSession, user_id: uuid.UUID) -> list[VirtualCard]:
    result = await session.exec(
        select(VirtualCard).where(VirtualCard.user_id == user_id).order_by(VirtualCard.created_at.desc())
    )
    cards = list(result.all())
    # A new card settles PROVISIONING -> active on Bitnob's side within
    # seconds, but only GET /cards/{id} used to sync it - and the Cards page
    # only ever lists. Without this a paid card showed "provisioning" forever.
    for card in cards:
        if card.status == CardStatus.PROVISIONING:
            await sync_card_status(session, card)
    return cards


async def freeze_card(session: AsyncSession, card: VirtualCard) -> VirtualCard:
    if card.status != CardStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Only an active card can be frozen")
    try:
        await bitnob_cards.set_card_status(card.bitnob_card_id, "frozen")
    except bitnob_cards.BitnobError as e:
        raise HTTPException(status_code=400, detail=e.user_message()) from e
    card.status = CardStatus.FROZEN
    card.updated_at = datetime.now(timezone.utc)
    session.add(card)
    await session.commit()
    await session.refresh(card)
    return card


async def unfreeze_card(session: AsyncSession, card: VirtualCard) -> VirtualCard:
    if card.status != CardStatus.FROZEN:
        raise HTTPException(status_code=400, detail="Only a frozen card can be unfrozen")
    try:
        await bitnob_cards.set_card_status(card.bitnob_card_id, "active")
    except bitnob_cards.BitnobError as e:
        raise HTTPException(status_code=400, detail=e.user_message()) from e
    card.status = CardStatus.ACTIVE
    card.updated_at = datetime.now(timezone.utc)
    session.add(card)
    await session.commit()
    await session.refresh(card)
    return card


async def terminate_card(session: AsyncSession, card: VirtualCard, data: CardTerminate) -> VirtualCard:
    if card.status in (CardStatus.TERMINATED, CardStatus.PENDING_PAYMENT, CardStatus.PROVISIONING):
        raise HTTPException(status_code=400, detail=f"Cannot terminate a card in status '{card.status}'")
    # Last known balance, as a fallback if Bitnob's response doesn't say what it returned.
    balance_before = card.balance
    try:
        card_data = (await bitnob_cards.get_card(card.bitnob_card_id)).get("data", {}).get("card", {})
        balance_before = bitnob_cards.from_micro_units(int(card_data.get("balance_amount", 0)))
    except (bitnob_cards.BitnobError, ValueError, TypeError):
        pass
    try:
        # Bitnob blocks termination within 24h of creation - surfaces as a
        # normal BitnobError here, not a special case, since it's just
        # another validation rule from their side.
        result = await bitnob_cards.terminate_card(card.bitnob_card_id, data.reason)
    except bitnob_cards.BitnobError as e:
        raise HTTPException(status_code=400, detail=e.user_message()) from e

    # Bitnob sends the leftover balance to the company wallet; pay it to the owner.
    returned = (result or {}).get("data") or {}
    refund_usd = balance_before
    try:
        if returned.get("remaining_balance") is not None:
            refund_usd = bitnob_cards.from_micro_units(int(returned["remaining_balance"]))
    except (ValueError, TypeError):
        pass
    if returned.get("balance_returned") is False:
        refund_usd = Decimal("0")
    paid = await termination_payout.start_termination_payout(session, card.id, refund_usd, source="owner")
    return paid or card


async def list_card_transactions(session: AsyncSession, card: VirtualCard) -> list[CardTransactionRead]:
    """Build a unified transaction history from the card's initial load and
    (when the card has been issued) Bitnob's card transactions feed."""
    items: list[CardTransactionRead] = []

    # Initial card funding / creation payment
    if card.payment_reference or card.initial_funding_ghs:
        status = card.status.value if hasattr(card.status, "value") else str(card.status)
        kind = "refund" if card.refunded_at else "initial_funding"
        direction = "credit" if not card.refunded_at else "debit"
        desc = (
            "Initial card funding refunded"
            if card.refunded_at
            else "Initial card funding"
        )
        if card.fee_ghs:
            desc = f"{desc} (+ GHS {card.fee_ghs} card fees)"
        items.append(
            CardTransactionRead(
                id=f"local-initial-{card.id}",
                card_id=card.id,
                kind=kind,
                direction=direction,
                amount=card.initial_funding_ghs,
                currency="GHS",
                amount_ghs=card.initial_funding_ghs,
                amount_usd=_ghs_to_usd(card.initial_funding_ghs),
                status=status,
                description=desc,
                reference=card.payment_reference or card.refund_reference,
                source="local",
                created_at=card.created_at,
            )
        )

    # Provider-side spend / network activity (sandbox or live Bitnob)
    if card.bitnob_card_id:
        try:
            remote = await bitnob_cards.list_transactions(card.bitnob_card_id)
            rows = remote
            if isinstance(remote, dict):
                rows = remote.get("data") or remote.get("transactions") or remote.get("results") or []
            # Bitnob nests the list one level down ({"data": {"transactions": [...]}},
            # confirmed live) - the dict used to be dropped here, so no Bitnob-side
            # transaction ever reached the app.
            if isinstance(rows, dict):
                rows = rows.get("transactions") or rows.get("results") or []
            if not isinstance(rows, list):
                rows = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                txn_id = str(row.get("id") or row.get("transaction_id") or row.get("reference") or "")
                amount = None
                currency = str(row.get("currency") or "USD")
                try:
                    display = row.get("display_amount") if row.get("display_amount") is not None else row.get("displayAmount")
                    if display is not None:
                        amount = Decimal(str(display)).quantize(Decimal("0.01"))
                    elif row.get("amount") is not None:
                        # `amount` is always micro-units (1 USD = 1,000,000) and
                        # arrives as a STRING (e.g. "1000000") - the old int-only
                        # check showed a $1 fee as "1000000 USD".
                        amount = bitnob_cards.from_micro_units(abs(int(Decimal(str(row["amount"])))))
                except Exception:
                    amount = None
                txn_type = str(
                    row.get("transaction_type")
                    or row.get("type")
                    or row.get("event_type")
                    or "other"
                ).lower()
                if any(x in txn_type for x in ("debit", "spend", "settlement", "authorization", "cross")):
                    direction = "debit"
                    kind = "debit"
                elif any(x in txn_type for x in ("credit", "fund", "refund", "revers")):
                    direction = "credit"
                    kind = "credit" if "refund" not in txn_type else "refund"
                elif "declin" in txn_type:
                    direction = "neutral"
                    kind = "decline"
                else:
                    direction = "neutral"
                    kind = "other"
                created = row.get("created_at") or row.get("timestamp") or row.get("date")
                if isinstance(created, (int, float)):
                    from datetime import datetime, timezone
                    created_at = datetime.fromtimestamp(created, tz=timezone.utc)
                elif isinstance(created, str):
                    from datetime import datetime
                    try:
                        created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    except Exception:
                        created_at = card.updated_at
                else:
                    created_at = card.updated_at
                merchant = row.get("merchant_name") or row.get("merchant")
                desc = str(row.get("description") or merchant or txn_type or "Card transaction")
                items.append(
                    CardTransactionRead(
                        id=f"bitnob-{txn_id or created_at.isoformat()}",
                        card_id=card.id,
                        kind=kind,
                        direction=direction,
                        amount=amount,
                        currency=currency,
                        amount_usd=amount if currency.upper() == "USD" else None,
                        status=str(row.get("status") or txn_type),
                        description=desc,
                        merchant_name=str(merchant) if merchant else None,
                        reference=str(row.get("reference") or txn_id or "") or None,
                        source="bitnob",
                        created_at=created_at,
                    )
                )
        except bitnob_cards.BitnobError:
            # Credentials missing or sandbox empty - local history still returned
            pass
        except Exception:
            pass

    # Declines never appear in Bitnob's transaction feed; they only arrive as
    # webhooks (card_events), so add them here or the cardholder can't see why
    # a payment failed.
    declined = await session.exec(
        select(CardEvent)
        .where(CardEvent.card_id == card.id, CardEvent.event_type.contains("declin"))
        .order_by(CardEvent.created_at.desc())
    )
    for ev in declined.all():
        items.append(
            CardTransactionRead(
                id=f"event-{ev.event_id}",
                card_id=card.id,
                kind="decline",
                direction="neutral",
                amount=ev.amount_usd,
                currency="USD",
                amount_usd=ev.amount_usd,
                status="declined",
                description=f"Declined{': ' + ev.reason if ev.reason else ''}",
                merchant_name=ev.merchant,
                reference=ev.reference,
                source="bitnob",
                created_at=ev.created_at,
            )
        )

    items.sort(key=lambda t: t.created_at or card.created_at, reverse=True)
    return items
