"""Tracking Paystack refunds through to their real outcome.

Paystack refunds are asynchronous: POST /refund only acknowledges the
request. Each refundable record (virtual card creation, card top-up,
cross-border transfer) therefore goes DELIVERY_FAILED -> REFUND_PENDING when
the refund is requested, and only reaches REFUNDED when Paystack reports it
processed - via the refund.processed webhook or the reconcile sweep polling
GET /refund/{id}. A failed refund goes back to DELIVERY_FAILED so the user
(or support) can request it again instead of the app claiming money was
returned when it wasn't.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable

from fastapi import HTTPException
from sqlmodel import SQLModel, or_, select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.models import User
from src.cards.models import CardFunding, CardStatus, FundingStatus, VirtualCard
from src.common.locking import locked_first
from src.common.sms import send_sms
from src.crossborder.models import CrossBorderStatus, CrossBorderTransfer
from src.payments import paystack

logger = logging.getLogger(__name__)


async def _card_owner(session: AsyncSession, card: VirtualCard) -> User:
    return await session.get(User, card.user_id)


async def _funding_owner(session: AsyncSession, funding: CardFunding) -> User:
    card = await session.get(VirtualCard, funding.card_id)
    return await session.get(User, card.user_id)


async def _crossborder_owner(session: AsyncSession, transfer: CrossBorderTransfer) -> User:
    return await session.get(User, transfer.sender_id)


@dataclass(frozen=True)
class RefundKind:
    name: str
    model: type[SQLModel]
    refundable: Any  # DELIVERY_FAILED - the only state a refund can start from
    pending: Any  # REFUND_PENDING
    refunded: Any  # REFUNDED
    amount_field: str  # the GHS amount charged, refunded in full
    what: str  # user-facing noun for SMS
    owner: Callable[[AsyncSession, Any], Awaitable[User]]


CARD_CREATION = RefundKind("card_creation", VirtualCard, CardStatus.DELIVERY_FAILED, CardStatus.REFUND_PENDING,
                           CardStatus.REFUNDED, "initial_funding_ghs", "virtual card", _card_owner)
CARD_FUNDING = RefundKind("card_funding", CardFunding, FundingStatus.DELIVERY_FAILED, FundingStatus.REFUND_PENDING,
                          FundingStatus.REFUNDED, "amount_ghs", "card top-up", _funding_owner)
CROSSBORDER = RefundKind("crossborder_transfer", CrossBorderTransfer, CrossBorderStatus.DELIVERY_FAILED,
                         CrossBorderStatus.REFUND_PENDING, CrossBorderStatus.REFUNDED, "source_amount",
                         "cross-border transfer", _crossborder_owner)
REFUND_KINDS = (CARD_CREATION, CARD_FUNDING, CROSSBORDER)


def _touch(row) -> None:
    if hasattr(row, "updated_at"):
        row.updated_at = datetime.now(timezone.utc)


async def start_refund(session: AsyncSession, kind: RefundKind, row_id) -> Any:
    """Request a full refund of the row's charge. Locked, so a double-tapped
    Refund can't issue two refunds for one charge."""
    row = await locked_first(session, kind.model, kind.model.id == row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    if row.status != kind.refundable:
        raise HTTPException(
            status_code=400, detail=f"Only a {kind.what} stuck after payment (delivery_failed) can be refunded"
        )

    amount: Decimal = getattr(row, kind.amount_field)
    try:
        refund = await paystack.refund_transaction(row.payment_reference, amount)
    except paystack.PaystackError as e:
        raise HTTPException(status_code=400, detail=f"Refund failed: {e}") from e

    row.status = kind.pending
    row.refund_reference = str(refund.get("id", row.payment_reference))
    row.refunded_at = None  # set only once Paystack reports it processed
    _touch(row)
    session.add(row)
    await session.commit()
    await session.refresh(row)

    owner = await kind.owner(session, row)
    await send_sms(
        owner.phone_number,
        f"Your {kind.what} couldn't be completed, so we've started a refund of your GHS {amount} payment. "
        f"It can take a few days to reach you.",
    )

    # Rare, but Paystack can report a refund as already processed.
    if refund.get("status") in paystack.REFUND_FINAL_STATUSES:
        await apply_refund_outcome(session, kind, row.refund_reference, refund["status"], refund.get("reason"))
        await session.refresh(row)
    return row


async def apply_refund_outcome(
    session: AsyncSession, kind: RefundKind, refund_reference: str | None, outcome: str,
    reason: str | None = None, transaction_reference: str | None = None,
) -> Any:
    """processed -> REFUNDED; failed -> back to DELIVERY_FAILED. Idempotent:
    only a row still REFUND_PENDING changes, so duplicate webhooks and the
    sweep racing a webhook are harmless."""
    matchers = []
    if refund_reference:
        matchers.append(kind.model.refund_reference == str(refund_reference))
    if transaction_reference:
        matchers.append(kind.model.payment_reference == transaction_reference)
    if not matchers:
        return None
    row = await locked_first(session, kind.model, or_(*matchers), kind.model.status == kind.pending)
    if row is None:
        return None

    amount = getattr(row, kind.amount_field)
    owner = await kind.owner(session, row)
    if outcome == "processed":
        row.status = kind.refunded
        row.refunded_at = datetime.now(timezone.utc)
        message = f"Your GHS {amount} refund for your {kind.what} has been completed."
    else:
        logger.warning("Refund %s for %s %s failed: %s", row.refund_reference, kind.name, row.id, reason)
        row.status = kind.refundable
        row.failure_reason = f"Refund failed{': ' + reason if reason else ''} - you can request it again"
        row.refund_reference = None  # a new refund can be started
        message = (f"Your GHS {amount} refund for your {kind.what} didn't go through. "
                   f"Open the app to request it again.")
    _touch(row)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    await send_sms(owner.phone_number, message)
    return row


async def handle_refund_webhook(session: AsyncSession, event: str, data: dict) -> None:
    """refund.processed / refund.failed. Paystack's refund payload identifies
    the original charge by transaction_reference; the refund's own id is
    matched too when present."""
    outcome = {"refund.processed": "processed", "refund.failed": "failed"}.get(event)
    if outcome is None:
        return  # refund.pending / refund.processing - nothing to do yet
    refund_id = data.get("id") or data.get("refund_id")
    transaction = data.get("transaction")
    transaction_reference = data.get("transaction_reference") or (
        transaction.get("reference") if isinstance(transaction, dict) else None
    )
    for kind in REFUND_KINDS:
        row = await apply_refund_outcome(
            session, kind, str(refund_id) if refund_id else None, outcome,
            data.get("reason") or data.get("merchant_note"), transaction_reference,
        )
        if row is not None:
            return


async def sweep_pending_refunds(session: AsyncSession) -> int:
    """Poll Paystack for every REFUND_PENDING row. Returns how many settled."""
    changed = 0
    for kind in REFUND_KINDS:
        pending = (
            await session.exec(
                select(kind.model.id, kind.model.refund_reference).where(
                    kind.model.status == kind.pending, kind.model.refund_reference.is_not(None)
                )
            )
        ).all()
        for row_id, refund_reference in pending:
            try:
                refund = await paystack.fetch_refund(refund_reference)
                status = refund.get("status")
                if status in paystack.REFUND_FINAL_STATUSES:
                    if await apply_refund_outcome(session, kind, refund_reference, status, refund.get("reason")):
                        changed += 1
            except paystack.PaystackError as exc:
                logger.info("Refund check skipped for %s %s: %s", kind.name, row_id, exc)
                await session.rollback()
            except Exception:
                logger.exception("Refund check failed for %s %s", kind.name, row_id)
                await session.rollback()
    return changed
