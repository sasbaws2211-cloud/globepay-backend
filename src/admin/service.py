import uuid
from decimal import Decimal

from fastapi import HTTPException
from sqlmodel import func, or_, select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.admin.models import AdminAuditLog
from src.admin.schemas import AdminUserDetail, AuditLogEntry, PlatformStats, StuckTransaction
from src.auth.models import KycTier, User
from src.cards.models import CardStatus, VirtualCard
from src.crossborder.models import CrossBorderStatus, CrossBorderTransfer
from src.vaults.models import Vault, VaultWithdrawal, WithdrawalStatus
from src.wallet.models import TransferStatus, WalletTransfer


async def log_admin_action(
    session: AsyncSession,
    admin_user_id: uuid.UUID,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    details: dict | None = None,
) -> None:
    session.add(
        AdminAuditLog(
            admin_user_id=admin_user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
        )
    )
    await session.commit()


async def list_audit_log(session: AsyncSession, limit: int = 100) -> list[AuditLogEntry]:
    result = await session.exec(select(AdminAuditLog).order_by(AdminAuditLog.seq.desc()).limit(limit))
    entries = list(result.all())

    admins: dict[uuid.UUID, User] = {}
    output: list[AuditLogEntry] = []
    for entry in entries:
        if entry.admin_user_id not in admins:
            admins[entry.admin_user_id] = await session.get(User, entry.admin_user_id)
        admin = admins[entry.admin_user_id]
        output.append(
            AuditLogEntry(
                id=entry.id,
                admin_user_id=entry.admin_user_id,
                admin_name=admin.full_name if admin else "(deleted admin)",
                action=entry.action,
                target_type=entry.target_type,
                target_id=entry.target_id,
                details=entry.details,
                created_at=entry.created_at,
            )
        )
    return output


async def search_users(session: AsyncSession, query: str | None, limit: int = 50) -> list[User]:
    stmt = select(User).order_by(User.created_at.desc()).limit(limit)
    if query:
        pattern = f"%{query}%"
        stmt = (
            select(User)
            .where(or_(User.phone_number.ilike(pattern), User.full_name.ilike(pattern), User.email.ilike(pattern)))
            .order_by(User.created_at.desc())
            .limit(limit)
        )
    result = await session.exec(stmt)
    return list(result.all())


async def get_user_detail(session: AsyncSession, user_id: uuid.UUID) -> AdminUserDetail:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    vault_result = await session.exec(
        select(func.count(), func.coalesce(func.sum(Vault.balance), 0)).where(Vault.owner_id == user_id)
    )
    vault_count, total_vault_balance = vault_result.one()

    return AdminUserDetail(
        id=user.id,
        phone_number=user.phone_number,
        full_name=user.full_name,
        email=user.email,
        is_active=user.is_active,
        is_admin=user.is_admin,
        kyc_tier=user.kyc_tier,
        created_at=user.created_at,
        vault_count=vault_count,
        total_vault_balance=total_vault_balance,
    )


async def set_user_active_status(session: AsyncSession, user_id: uuid.UUID, is_active: bool) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = is_active
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def set_user_kyc_tier(session: AsyncSession, user_id: uuid.UUID, tier: KycTier) -> tuple[User, KycTier]:
    """Change a user's verification tier (and so their transaction limits).
    ID_VERIFIED is only reachable this way - there's no self-service ID check.
    Returns the user and their previous tier, for the audit log."""
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    previous = user.kyc_tier
    user.kyc_tier = tier
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user, previous


async def get_crossborder_transfer(session: AsyncSession, transfer_id: uuid.UUID) -> CrossBorderTransfer:
    transfer = await session.get(CrossBorderTransfer, transfer_id)
    if transfer is None:
        raise HTTPException(status_code=404, detail="Transfer not found")
    return transfer


async def get_card(session: AsyncSession, card_id: uuid.UUID) -> VirtualCard:
    card = await session.get(VirtualCard, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return card


async def list_stuck_transactions(session: AsyncSession) -> list[StuckTransaction]:
    """Everything currently sitting in DELIVERY_FAILED across the sandbox
    Bitnob-backed features - money already collected via Paystack, nothing
    delivered, and (per src/crossborder/service.py and src/cards/service.py)
    waiting on a support agent to retry or refund since only the owning
    user could otherwise trigger those actions."""
    items: list[StuckTransaction] = []

    cb_result = await session.exec(
        select(CrossBorderTransfer).where(CrossBorderTransfer.status == CrossBorderStatus.DELIVERY_FAILED)
    )
    for transfer in cb_result.all():
        user = await session.get(User, transfer.sender_id)
        items.append(
            StuckTransaction(
                kind="crossborder_transfer",
                id=transfer.id,
                user_id=transfer.sender_id,
                user_phone=user.phone_number,
                amount=transfer.source_amount,
                status=transfer.status.value,
                failure_reason=transfer.failure_reason,
                retry_count=transfer.retry_count,
                created_at=transfer.created_at,
            )
        )

    card_result = await session.exec(select(VirtualCard).where(VirtualCard.status == CardStatus.DELIVERY_FAILED))
    for card in card_result.all():
        user = await session.get(User, card.user_id)
        items.append(
            StuckTransaction(
                kind="card_creation",
                id=card.id,
                user_id=card.user_id,
                user_phone=user.phone_number,
                amount=card.initial_funding_ghs,
                status=card.status.value,
                failure_reason=card.failure_reason,
                retry_count=card.retry_count,
                created_at=card.created_at,
            )
        )

    items.sort(key=lambda item: item.created_at)
    return items


async def get_platform_stats(session: AsyncSession) -> PlatformStats:
    total_users = (await session.exec(select(func.count()).select_from(User))).one()

    vault_result = await session.exec(select(func.count(), func.coalesce(func.sum(Vault.balance), 0)))
    total_vaults, total_vault_balance = vault_result.one()

    # Fee revenue only counts where the fee was actually realized:
    # VaultWithdrawal rows only exist once a real payout fired
    # (already-confirmed-paid balance), but a WalletTransfer row is created
    # up front, before the sender's charge is even confirmed - counting
    # every row there would count fees on money that was never collected.
    # A FAILED withdrawal's payout bounced and its gross was restored to the
    # vault, so its fee was never realized.
    vault_fees: Decimal = (
        await session.exec(
            select(func.coalesce(func.sum(VaultWithdrawal.platform_fee), 0)).where(
                VaultWithdrawal.status != WithdrawalStatus.FAILED
            )
        )
    ).one()
    wallet_fees: Decimal = (
        await session.exec(
            select(func.coalesce(func.sum(WalletTransfer.platform_fee), 0)).where(
                WalletTransfer.status.in_([
                    TransferStatus.COMPLETED,
                    TransferStatus.AWAITING_RECIPIENT_PAYOUT_INFO,
                    TransferStatus.PAYOUT_PENDING,
                ])
            )
        )
    ).one()
    total_fees_collected = vault_fees + wallet_fees

    stuck = await list_stuck_transactions(session)

    return PlatformStats(
        total_users=total_users,
        total_vaults=total_vaults,
        total_vault_balance=total_vault_balance,
        total_fees_collected=total_fees_collected,
        stuck_transaction_count=len(stuck),
    )
