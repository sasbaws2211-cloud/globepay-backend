import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.models import User
from src.cards import bitnob_cards
from src.cards.models import CardFunding, CardStatus, FundingStatus, VirtualCard
from src.cards.schemas import CardCreate, CardFundingCreate, CardTerminate, CardTransactionRead
from src.common.kyc_limits import check_transaction_limit, record_transaction_volume
from src.common.locking import locked_first
from src.common.sms import send_sms
from src.config import settings
from src.payments import paystack, refunds

MIN_CARD_USD = Decimal("2.00")  # confirmed live: Bitnob rejects card creation below $2
# Every card this app issues is a Bitnob *lite* card: loaded once, at
# creation, up to this cap, and never topped up (see bitnob_cards).
MAX_CARD_LOAD_USD = Decimal(bitnob_cards.LITE_MAX_LOAD_USD)
MAX_TOPUP_USD = MAX_CARD_LOAD_USD  # kept for the (now always-refused) top-up path
MAX_DELIVERY_RETRIES = 3


def _ghs_to_usd(amount_ghs: Decimal) -> Decimal:
    return (amount_ghs / Decimal(str(settings.DEMO_GHS_USD_RATE))).quantize(Decimal("0.01"))


def _usd_to_ghs(amount_usd: Decimal) -> Decimal:
    return (amount_usd * Decimal(str(settings.DEMO_GHS_USD_RATE))).quantize(Decimal("0.01"))


def get_card_limits() -> dict:
    """What the app shows before someone pays - single source of truth so the
    UI can't drift from what the backend (and Bitnob) will accept."""
    return {
        "card_type": "lite",
        "can_top_up": False,
        # Round the GHS minimum up so the converted USD never lands a pesewa under $2.
        "min_load_ghs": (_usd_to_ghs(MIN_CARD_USD) + Decimal("0.01")),
        "max_load_ghs": _usd_to_ghs(MAX_CARD_LOAD_USD),
        "min_load_usd": MIN_CARD_USD,
        "max_load_usd": MAX_CARD_LOAD_USD,
        "max_cards_per_phone": bitnob_cards.MAX_LITE_CARDS_PER_CUSTOMER,
        "creation_fee_usd": Decimal(bitnob_cards.CARD_CREATION_FEE_USD),
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
    closed if Bitnob can't be reached, same reasoning as the top-up check."""
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
    await check_transaction_limit(session, user, data.initial_funding_ghs)

    amount_usd = _ghs_to_usd(data.initial_funding_ghs)
    limits = get_card_limits()
    if amount_usd < MIN_CARD_USD:
        raise HTTPException(status_code=400, detail=f"Minimum card load is GHS {limits['min_load_ghs']} (${MIN_CARD_USD} equivalent)")
    if amount_usd > MAX_CARD_LOAD_USD:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum card load is GHS {limits['max_load_ghs']} (${MAX_CARD_LOAD_USD}) - "
            f"cards are loaded once and can't be topped up later",
        )

    local_phone = _normalize_local_phone(data.dial_code, data.local_phone_number)
    if not local_phone:
        raise HTTPException(status_code=400, detail="Enter the card phone number")
    await _ensure_card_slot_available(local_phone)

    reference = f"card-{uuid.uuid4().hex[:14]}"
    card = VirtualCard(
        user_id=user.id,
        initial_funding_ghs=data.initial_funding_ghs,
        payment_reference=reference,
        dial_code=data.dial_code,
        local_phone_number=local_phone,
    )
    session.add(card)
    await session.commit()

    checkout = await paystack.initialize_transaction(
        email=data.sender_email,
        amount=data.initial_funding_ghs,
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
    record_transaction_volume(session, user.id, card.initial_funding_ghs, "card_creation")
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
    try:
        # Bitnob blocks termination within 24h of creation - surfaces as a
        # normal BitnobError here, not a special case, since it's just
        # another validation rule from their side.
        await bitnob_cards.terminate_card(card.bitnob_card_id, data.reason)
    except bitnob_cards.BitnobError as e:
        raise HTTPException(status_code=400, detail=e.user_message()) from e
    card.status = CardStatus.TERMINATED
    card.updated_at = datetime.now(timezone.utc)
    session.add(card)
    await session.commit()
    await session.refresh(card)
    return card


async def _ensure_card_accepts_topups(card: VirtualCard) -> None:
    """Refuse a top-up BEFORE the Paystack checkout, not after the money is taken.

    Confirmed live 2026-09-29: Bitnob rejects every top-up on a lite card
    ("topups are not supported for lite cards") - a lite card is funded once,
    at creation, and create_lite_card is the only way this app issues cards.
    The card's type is read from Bitnob rather than assumed, so a card type
    that does support top-ups keeps working if one is ever issued. If Bitnob
    can't be reached, fail closed: a refused top-up is recoverable, a charge
    that can't be delivered needs a refund."""
    if card.bitnob_card_id is None:
        raise HTTPException(status_code=400, detail="This card can't be topped up")
    try:
        card_data = (await bitnob_cards.get_card(card.bitnob_card_id)).get("data", {}).get("card", {})
    except bitnob_cards.BitnobError as e:
        raise HTTPException(
            status_code=503, detail="Couldn't confirm this card can be topped up right now - please try again shortly"
        ) from e
    if card_data.get("card_type") == "lite":
        raise HTTPException(
            status_code=400,
            detail="This card can't be topped up - it's a lite card, which is funded once when it's created. "
            "Create a new card to add more funds.",
        )


async def initiate_card_funding(session: AsyncSession, card: VirtualCard, data: CardFundingCreate) -> dict:
    if card.status != CardStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Can only fund an active card")

    await _ensure_card_accepts_topups(card)

    owner = await session.get(User, card.user_id)
    await check_transaction_limit(session, owner, data.amount_ghs)

    amount_usd = _ghs_to_usd(data.amount_ghs)
    # Confirmed live: Bitnob enforces the same $2 minimum on top-ups as on
    # initial card creation - caught by a real funding request failing
    # with "amount below minimum funding of $2" after payment had already
    # been collected, which is exactly the failure mode worth checking for
    # upfront instead of after the user's paid.
    if amount_usd < MIN_CARD_USD:
        raise HTTPException(status_code=400, detail=f"Minimum top-up is ~GHS {MIN_CARD_USD * Decimal(str(settings.DEMO_GHS_USD_RATE)):.2f} (${MIN_CARD_USD} equivalent)")
    if amount_usd > MAX_TOPUP_USD:
        raise HTTPException(status_code=400, detail=f"Maximum top-up is ~GHS {MAX_TOPUP_USD * Decimal(str(settings.DEMO_GHS_USD_RATE)):.2f} (${MAX_TOPUP_USD} equivalent)")

    reference = f"cardtopup-{uuid.uuid4().hex[:14]}"
    funding = CardFunding(
        card_id=card.id, amount_ghs=data.amount_ghs, amount_usd=amount_usd, payment_reference=reference
    )
    session.add(funding)
    await session.commit()

    checkout = await paystack.initialize_transaction(
        email=data.sender_email,
        amount=data.amount_ghs,
        reference=reference,
        metadata={"type": "card_funding", "funding_id": str(funding.id)},
    )
    return {"authorization_url": checkout["authorization_url"], "reference": reference}


async def list_card_fundings(session: AsyncSession, card: VirtualCard) -> list[CardFunding]:
    result = await session.exec(
        select(CardFunding)
        .where(CardFunding.card_id == card.id)
        .order_by(CardFunding.created_at.desc())
    )
    return list(result.all())


async def get_card_funding(session: AsyncSession, card: VirtualCard, funding_id: uuid.UUID) -> CardFunding:
    funding = await session.get(CardFunding, funding_id)
    if funding is None or funding.card_id != card.id:
        raise HTTPException(status_code=404, detail="Card funding not found")
    return funding


async def _attempt_card_funding(funding: CardFunding, card: VirtualCard) -> None:
    """Mutates funding.status/failure_reason in place. Never raises: the
    GHS payment has already been collected by the time this runs (both on
    first attempt and on retry), so any failure here must land in
    DELIVERY_FAILED, not bubble up."""
    try:
        await bitnob_cards.fund_card(card.bitnob_card_id, funding.amount_usd, funding.payment_reference)
        funding.status = FundingStatus.COMPLETED
    except bitnob_cards.BitnobError as e:
        funding.status = FundingStatus.DELIVERY_FAILED
        funding.failure_reason = f"Bitnob sandbox call failed: {e.user_message()}"


async def confirm_card_funding(session: AsyncSession, reference: str) -> CardFunding:
    # Locked: webhook, refresh poll and reconcile sweep may all race here.
    funding = await locked_first(session, CardFunding, CardFunding.payment_reference == reference)
    if funding is None:
        raise HTTPException(status_code=404, detail="Card funding not found")

    if funding.status != FundingStatus.PENDING_PAYMENT:
        return funding

    verified = await paystack.verify_transaction(reference)
    if verified.get("status") != "success":
        funding.status = FundingStatus.FAILED
        funding.failure_reason = "GHS payment not successful"
        session.add(funding)
        await session.commit()
        return funding

    card = await session.get(VirtualCard, funding.card_id)
    record_transaction_volume(session, card.user_id, funding.amount_ghs, "card_funding")
    await _attempt_card_funding(funding, card)

    session.add(funding)
    await session.commit()
    await session.refresh(funding)

    if funding.status == FundingStatus.COMPLETED:
        await sync_card_status(session, card)

    return funding


async def retry_card_funding(session: AsyncSession, card: VirtualCard, funding: CardFunding) -> CardFunding:
    if funding.status != FundingStatus.DELIVERY_FAILED:
        raise HTTPException(
            status_code=400, detail="Only a top-up stuck after payment (delivery_failed) can be retried"
        )
    if funding.retry_count >= MAX_DELIVERY_RETRIES:
        raise HTTPException(
            status_code=400,
            detail=f"Retry limit reached ({MAX_DELIVERY_RETRIES}) - request a refund instead",
        )
    # A top-up on a lite card fails the same way every time, so a retry would
    # only burn an attempt - send the user to Refund instead.
    try:
        await _ensure_card_accepts_topups(card)
    except HTTPException as e:
        if e.status_code == 400:
            raise HTTPException(
                status_code=400, detail="This card can't be topped up, so this payment can't be delivered - request a refund instead"
            ) from e
        raise

    funding.retry_count += 1
    await _attempt_card_funding(funding, card)
    session.add(funding)
    await session.commit()
    await session.refresh(funding)

    if funding.status == FundingStatus.COMPLETED:
        await sync_card_status(session, card)
        user = await session.get(User, card.user_id)
        await send_sms(user.phone_number, "Good news - your card top-up went through on retry.")
    return funding


async def refund_card_funding(session: AsyncSession, funding: CardFunding) -> CardFunding:
    """Moves the top-up to REFUND_PENDING; REFUNDED only once Paystack reports
    the refund processed - see src/payments/refunds.py."""
    return await refunds.start_refund(session, refunds.CARD_FUNDING, funding.id)


async def list_card_transactions(session: AsyncSession, card: VirtualCard) -> list[CardTransactionRead]:
    """Build a unified transaction history from local funding records and
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

    fundings = await list_card_fundings(session, card)
    for f in fundings:
        st = f.status.value if hasattr(f.status, "value") else str(f.status)
        if f.refunded_at or st.lower() in ("refunded",):
            kind, direction, desc = "refund", "debit", "Top-up refunded"
        elif st.lower() in ("completed",):
            kind, direction, desc = "funding", "credit", "Card top-up"
        elif "fail" in st.lower():
            kind, direction, desc = "funding", "credit", f"Top-up failed"
        else:
            kind, direction, desc = "funding", "credit", "Top-up pending"
        if f.failure_reason:
            desc = f"{desc}: {f.failure_reason}"
        items.append(
            CardTransactionRead(
                id=f"local-funding-{f.id}",
                card_id=card.id,
                kind=kind,
                direction=direction,
                amount=f.amount_ghs,
                currency="GHS",
                amount_ghs=f.amount_ghs,
                amount_usd=f.amount_usd,
                status=st,
                description=desc,
                reference=f.payment_reference or f.refund_reference,
                source="local",
                created_at=f.created_at,
            )
        )

    # Provider-side spend / network activity (sandbox or live Bitnob)
    if card.bitnob_card_id:
        try:
            remote = await bitnob_cards.list_transactions(card.bitnob_card_id)
            rows = remote
            if isinstance(remote, dict):
                rows = remote.get("data") or remote.get("transactions") or remote.get("results") or []
            if not isinstance(rows, list):
                rows = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                txn_id = str(row.get("id") or row.get("transaction_id") or row.get("reference") or "")
                amount_raw = row.get("amount") or row.get("display_amount")
                amount = None
                currency = str(row.get("currency") or "USD")
                if amount_raw is not None:
                    try:
                        # Bitnob often uses micro-units for amount
                        if isinstance(amount_raw, int) and abs(amount_raw) > 1000:
                            amount = bitnob_cards.from_micro_units(abs(amount_raw))
                        else:
                            amount = Decimal(str(amount_raw))
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

    items.sort(key=lambda t: t.created_at or card.created_at, reverse=True)
    return items
