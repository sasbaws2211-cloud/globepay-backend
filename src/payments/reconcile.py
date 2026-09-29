"""Background reconciliation for in-flight Paystack charges and payouts.

Webhooks are the primary signal, but they can be delayed, dropped, or (in
local dev) unable to reach this server at all. This sweep asks Paystack
directly about anything still in flight and funnels the answer into the
same handlers the webhook uses - their row locks and status checks make a
sweep racing a webhook harmless.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.cards import service as card_service
from src.cards.models import CardFunding, CardStatus, FundingStatus, VirtualCard
from src.crossborder import service as crossborder_service
from src.crossborder.models import CrossBorderStatus, CrossBorderTransfer
from src.payments import paystack, refunds
from src.splitbill import service as splitbill_service
from src.splitbill.models import SharePayoutStatus, ShareStatus, SplitBillShare
from src.vaults import service as vault_service
from src.vaults.models import ContributionStatus, Vault, VaultContribution, VaultWithdrawal, WithdrawalStatus
from src.wallet import service as wallet_service
from src.wallet.models import TransferStatus, WalletTransfer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChargeKind:
    """One kind of checkout whose confirmation normally arrives by webhook."""

    name: str
    model: type[SQLModel]
    pending_status: Any
    confirm: Callable[[AsyncSession, str], Awaitable[Any]]  # the webhook's own confirm handler
    owner_id: Callable[[AsyncSession, Any], Awaitable[uuid.UUID]]
    # Whether created_at marks when the checkout started. A split-bill share
    # row is created with the bill, possibly long before anyone pays, so the
    # 24h age window would wrongly skip it.
    windowed: bool = True


async def _vault_owner(session: AsyncSession, contribution: VaultContribution) -> uuid.UUID:
    return (await session.get(Vault, contribution.vault_id)).owner_id


async def _card_owner(session: AsyncSession, funding: CardFunding) -> uuid.UUID:
    return (await session.get(VirtualCard, funding.card_id)).user_id


def _owner_field(attr: str) -> Callable[[AsyncSession, Any], Awaitable[uuid.UUID]]:
    async def get(_session: AsyncSession, row) -> uuid.UUID:
        return getattr(row, attr)
    return get


CHARGE_KINDS: list[ChargeKind] = [
    ChargeKind("vault_contribution", VaultContribution, ContributionStatus.PENDING,
               vault_service.confirm_contribution, _vault_owner),
    ChargeKind("splitbill_share", SplitBillShare, ShareStatus.PENDING,
               splitbill_service.confirm_share_payment, _owner_field("user_id"), windowed=False),
    ChargeKind("crossborder_transfer", CrossBorderTransfer, CrossBorderStatus.PENDING_PAYMENT,
               crossborder_service.confirm_transfer_payment, _owner_field("sender_id")),
    ChargeKind("card_creation", VirtualCard, CardStatus.PENDING_PAYMENT,
               card_service.confirm_card_payment, _owner_field("user_id")),
    ChargeKind("card_funding", CardFunding, FundingStatus.PENDING_PAYMENT,
               card_service.confirm_card_funding, _card_owner),
]


async def reconcile_charge(session: AsyncSession, kind: ChargeKind, reference: str) -> None:
    """Hand a charge to its confirm handler only once Paystack reports a final
    state. "abandoned"/"ongoing"/"pending" checkouts can still be paid, and
    every confirm handler marks anything other than success as FAILED - so
    calling it early would kill a payment the user is still making."""
    verified = await paystack.verify_transaction(reference)
    if verified.get("status") in ("success", "failed"):
        await kind.confirm(session, reference)


async def refresh_charge(session: AsyncSession, reference: str, user_id: uuid.UUID) -> dict | None:
    """Backs POST /payments/{reference}/refresh, polled by the app after a
    checkout. Finds which kind of payment the reference belongs to by looking
    it up (prefixes overlap, e.g. vault- vs vault-wd-), checks ownership, and
    reconciles it if it's still pending."""
    for kind in CHARGE_KINDS:
        row = (await session.exec(select(kind.model).where(kind.model.payment_reference == reference))).first()
        if row is None:
            continue
        if await kind.owner_id(session, row) != user_id:
            break  # someone else's payment - answer exactly as for an unknown reference
        if row.status == kind.pending_status:
            try:
                await reconcile_charge(session, kind, reference)
            except paystack.PaystackError as exc:
                logger.info("Refresh of %s %s: Paystack lookup failed: %s", kind.name, reference, exc)
                await session.rollback()
            row = (
                await session.exec(
                    select(kind.model).where(kind.model.payment_reference == reference)
                    .execution_options(populate_existing=True)
                )
            ).one()
        status = row.status.value if hasattr(row.status, "value") else str(row.status)
        return {"kind": kind.name, "status": status, "pending": row.status == kind.pending_status}
    return None

# Unpaid checkouts older than this are treated as abandoned and no longer polled.
CHARGE_POLL_WINDOW = timedelta(hours=24)
# Give the user a moment in the checkout before polling a fresh charge.
CHARGE_POLL_MIN_AGE = timedelta(minutes=1)
# Payouts held (e.g. at "otp") longer than this stop being polled automatically.
PAYOUT_POLL_WINDOW = timedelta(days=14)


async def _final_payout_event(reference: str) -> str | None:
    verified = await paystack.verify_transfer(reference)
    return paystack.TRANSFER_FINAL_EVENTS.get(verified.get("status"))


async def sweep_in_flight_payments(session: AsyncSession) -> int:
    """Returns how many items moved to a new state."""
    now = datetime.now(timezone.utc)
    changed = 0

    # Collect plain ids/references up front: a rollback after one failed item
    # expires every loaded row, and touching an expired row in async code
    # raises instead of lazy-loading.
    wallet_ids = (
        await session.exec(
            select(WalletTransfer.id).where(
                (
                    (WalletTransfer.status == TransferStatus.PENDING_PAYMENT)
                    & (WalletTransfer.created_at >= now - CHARGE_POLL_WINDOW)
                    & (WalletTransfer.created_at <= now - CHARGE_POLL_MIN_AGE)
                )
                | (
                    (WalletTransfer.status == TransferStatus.PAYOUT_PENDING)
                    & (WalletTransfer.created_at >= now - PAYOUT_POLL_WINDOW)
                )
            )
        )
    ).all()
    for transfer_id in wallet_ids:
        try:
            transfer = await session.get(WalletTransfer, transfer_id, populate_existing=True)
            before = transfer.status
            await wallet_service.reconcile_transfer(session, transfer)
            await session.refresh(transfer)
            changed += transfer.status != before
        except Exception:
            logger.exception("Reconcile failed for wallet transfer %s", transfer_id)
            await session.rollback()

    for kind in CHARGE_KINDS:
        query = select(kind.model.id, kind.model.payment_reference).where(
            kind.model.status == kind.pending_status,
            kind.model.payment_reference.is_not(None),
        )
        if kind.windowed:
            query = query.where(
                kind.model.created_at >= now - CHARGE_POLL_WINDOW,
                kind.model.created_at <= now - CHARGE_POLL_MIN_AGE,
            )
        pending = (await session.exec(query)).all()
        for row_id, reference in pending:
            try:
                await reconcile_charge(session, kind, reference)
                status = (await session.exec(select(kind.model.status).where(kind.model.id == row_id))).one()
                changed += status != kind.pending_status
            except paystack.PaystackError as exc:
                # Expected for references Paystack never saw (checkout never
                # opened, or seed/demo data) - not worth a traceback every sweep.
                logger.info("Reconcile skipped %s %s: %s", kind.name, row_id, exc)
                await session.rollback()
            except Exception:
                logger.exception("Reconcile failed for %s %s", kind.name, row_id)
                await session.rollback()

    changed += await refunds.sweep_pending_refunds(session)

    withdrawals = (
        await session.exec(
            select(VaultWithdrawal.id, VaultWithdrawal.payout_reference).where(
                VaultWithdrawal.status == WithdrawalStatus.PENDING,
                VaultWithdrawal.payout_reference.is_not(None),
                VaultWithdrawal.created_at >= now - PAYOUT_POLL_WINDOW,
            )
        )
    ).all()
    for withdrawal_id, reference in withdrawals:
        try:
            event = await _final_payout_event(reference)
            if event:
                await vault_service.handle_payout_event(session, event, reference)
                changed += 1
        except Exception:
            logger.exception("Reconcile failed for vault withdrawal %s", withdrawal_id)
            await session.rollback()

    shares = (
        await session.exec(
            select(SplitBillShare.id, SplitBillShare.payout_reference).where(
                SplitBillShare.payout_status == SharePayoutStatus.PENDING,
                SplitBillShare.payout_reference.is_not(None),
                SplitBillShare.paid_at >= now - PAYOUT_POLL_WINDOW,
            )
        )
    ).all()
    for share_id, reference in shares:
        try:
            event = await _final_payout_event(reference)
            if event:
                await splitbill_service.handle_payout_event(session, event, reference)
                changed += 1
        except Exception:
            logger.exception("Reconcile failed for split-bill share %s", share_id)
            await session.rollback()

    return changed
