"""Per-KYC-tier transaction volume limits - mirrors the shape of Bank of
Ghana's tiered e-money framework (higher verification unlocks higher
allowed volume). One shared ledger (TransactionVolumeLog) rather than
separate counters per module, so a user can't dodge the cap by spreading
one large amount across a vault contribution and a wallet transfer instead
of one big payment.

Two-step by design, split across two different points in each payment
flow:
  - check_transaction_limit() runs in the *initiate* step, before a
    Paystack checkout is even created, using confirmed volume plus any
    checkouts the user started recently but hasn't paid (see
    _in_flight_volume).
  - record_transaction_volume() runs in the *confirm* step (the webhook
    handler / refresh poll / reconcile sweep), only once Paystack has
    verified the charge actually succeeded.
Recording on confirmation rather than on initiation is deliberate: a
failed or abandoned checkout attempt must not permanently eat into a
real limit meant to cap money actually moved, not payment attempts.

Restored 2026-09-30: the original module's source was lost (only its
compiled .pyc survived); tiers, amounts, windows and messages are taken
from that bytecode. Money moving *inside* GlobePay - a round-up swept into
a vault, a referral bonus, a split bill withdrawn into a vault - isn't new
money in, so it isn't recorded again.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlmodel import Field, SQLModel, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.models import KycTier, User
from src.common.db_types import tz_aware_column
from src.config import settings

# (rolling 24-hour limit, rolling 30-day limit), GHS. None = no limit.
# PHONE_VERIFIED's daily figure is Paystack's maximum single transfer in Ghana
# (GHS 50,000 - https://support.paystack.com/hc/en-us/articles/360012276559);
# Paystack publishes no daily or monthly cap, so there's no monthly one here.
# Set 2026-09-30 (previously 5,000 / 20,000, ID_VERIFIED 20,000 / 100,000).
TIER_LIMITS: dict[KycTier, tuple[Decimal | None, Decimal | None]] = {
    KycTier.UNVERIFIED: (Decimal("500"), Decimal("2000")),
    KycTier.PHONE_VERIFIED: (Decimal("50000"), None),
    KycTier.ID_VERIFIED: (None, None),
}

# An unpaid checkout counts against the limit for this long, then it's
# treated as abandoned.
IN_FLIGHT_WINDOW = timedelta(minutes=30)


class TransactionVolumeLog(SQLModel, table=True):
    """One confirmed payment into GlobePay, for limit accounting."""

    __tablename__ = "transaction_volume_logs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    amount: Decimal = Field(max_digits=14, decimal_places=2)
    source: str  # wallet_transfer | vault_contribution | crossborder_transfer | card_creation | splitbill_share
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=tz_aware_column())


async def _confirmed_volume_since(session: AsyncSession, user_id: uuid.UUID, since: datetime) -> Decimal:
    result = await session.exec(
        select(func.coalesce(func.sum(TransactionVolumeLog.amount), 0)).where(
            TransactionVolumeLog.user_id == user_id, TransactionVolumeLog.created_at >= since
        )
    )
    return Decimal(result.one())


async def _in_flight_volume(session: AsyncSession, user_id: uuid.UUID, since: datetime) -> Decimal:
    """Money in checkouts this user started but hasn't paid yet.

    Only confirmed volume used to count, so someone could open several
    checkouts that each fit under the limit and then pay them all - blowing
    straight past it. Counting recent unpaid checkouts closes that; an
    abandoned one stops counting after IN_FLIGHT_WINDOW. (Split-bill shares
    are left out: a share row has no record of when its checkout started.)
    Imported lazily - each of these modules imports this one."""
    from src.cards.models import CardStatus, VirtualCard
    from src.crossborder.models import CrossBorderStatus, CrossBorderTransfer
    from src.vaults.models import ContributionStatus, Vault, VaultContribution
    from src.wallet.models import TransferStatus, WalletTransfer

    queries = [
        select(func.coalesce(func.sum(WalletTransfer.gross_amount + WalletTransfer.roundup_amount), 0)).where(
            WalletTransfer.sender_id == user_id,
            WalletTransfer.status == TransferStatus.PENDING_PAYMENT,
            WalletTransfer.created_at >= since,
        ),
        select(func.coalesce(func.sum(VaultContribution.amount), 0))
        .join(Vault, Vault.id == VaultContribution.vault_id)
        .where(
            Vault.owner_id == user_id,
            VaultContribution.status == ContributionStatus.PENDING,
            VaultContribution.created_at >= since,
        ),
        select(func.coalesce(func.sum(CrossBorderTransfer.source_amount), 0)).where(
            CrossBorderTransfer.sender_id == user_id,
            CrossBorderTransfer.status == CrossBorderStatus.PENDING_PAYMENT,
            CrossBorderTransfer.created_at >= since,
        ),
        select(func.coalesce(func.sum(VirtualCard.initial_funding_ghs + VirtualCard.fee_ghs), 0)).where(
            VirtualCard.user_id == user_id,
            VirtualCard.status == CardStatus.PENDING_PAYMENT,
            VirtualCard.created_at >= since,
        ),
    ]
    total = Decimal("0")
    for query in queries:
        total += Decimal((await session.exec(query)).one())
    return total


def limits_for(user: User) -> tuple[Decimal | None, Decimal | None]:
    return TIER_LIMITS[user.kyc_tier]


async def check_transaction_limit(session: AsyncSession, user: User, amount: Decimal) -> None:
    """Refuse (400) a payment that would take the user over their tier's
    24-hour or 30-day limit. Call before creating the checkout. A no-op while
    settings.ENFORCE_TRANSACTION_LIMITS is off (recording carries on)."""
    if not settings.ENFORCE_TRANSACTION_LIMITS:
        return
    daily_limit, monthly_limit = limits_for(user)
    if daily_limit is None and monthly_limit is None:
        return  # this tier has no limits
    now = datetime.now(timezone.utc)
    next_tier_hint = "phone number" if user.kyc_tier == KycTier.UNVERIFIED else "ID"

    amount = amount + await _in_flight_volume(session, user.id, now - IN_FLIGHT_WINDOW)

    if daily_limit is not None:
        daily_total = await _confirmed_volume_since(session, user.id, now - timedelta(hours=24))
        if daily_total + amount > daily_limit:
            raise HTTPException(
                status_code=400,
                detail=f"This would exceed your daily transaction limit of GHS {daily_limit} for your verification "
                f"level (payments you've started but not finished count too). Verify your {next_tier_hint} to raise it.",
            )

    if monthly_limit is None:
        return
    monthly_total = await _confirmed_volume_since(session, user.id, now - timedelta(days=30))
    if monthly_total + amount > monthly_limit:
        raise HTTPException(
            status_code=400,
            detail=f"This would exceed your monthly transaction limit of GHS {monthly_limit} for your verification "
            f"level. Verify your {next_tier_hint} to raise it.",
        )


def record_transaction_volume(session: AsyncSession, user_id: uuid.UUID, amount: Decimal, source: str) -> None:
    """Add a confirmed payment to the ledger. Part of the caller's
    transaction - it commits along with the payment being marked paid, so a
    payment is recorded exactly once (the confirm handlers are row-locked)."""
    if amount > 0:
        session.add(TransactionVolumeLog(user_id=user_id, amount=amount, source=source))


async def limits_summary(session: AsyncSession, user: User) -> dict:
    """For the app: the user's tier, limits and what's used so far."""
    daily_limit, monthly_limit = limits_for(user)
    now = datetime.now(timezone.utc)
    in_flight = await _in_flight_volume(session, user.id, now - IN_FLIGHT_WINDOW)
    daily_used = await _confirmed_volume_since(session, user.id, now - timedelta(hours=24)) + in_flight
    monthly_used = await _confirmed_volume_since(session, user.id, now - timedelta(days=30)) + in_flight
    return {
        "enforced": settings.ENFORCE_TRANSACTION_LIMITS,
        "kyc_tier": user.kyc_tier,
        # A limit (and its remaining) is None when the tier has no limit there.
        "daily_limit": daily_limit,
        "daily_used": daily_used,
        "daily_remaining": None if daily_limit is None else max(daily_limit - daily_used, Decimal("0")),
        "monthly_limit": monthly_limit,
        "monthly_used": monthly_used,
        "monthly_remaining": None if monthly_limit is None else max(monthly_limit - monthly_used, Decimal("0")),
    }
