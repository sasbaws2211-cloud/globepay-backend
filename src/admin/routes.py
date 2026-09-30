import uuid

from fastapi import APIRouter, Depends
from sqlmodel.ext.asyncio.session import AsyncSession

from src.admin import service
from src.admin.schemas import (
    AdminKycTierUpdate,
    AdminUserDetail,
    AdminUserStatusUpdate,
    AdminUserSummary,
    AuditLogEntry,
    PlatformStats,
    StuckTransaction,
)
from src.auth.dependencies import get_current_admin
from src.auth.models import User
from src.auth.schemas import UserRead
from src.cards import service as card_service
from src.cards.schemas import CardRead
from src.crossborder import service as crossborder_service
from src.crossborder.schemas import CrossBorderRead
from src.db.main import get_session

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(get_current_admin)])


@router.get("/stats", response_model=PlatformStats)
async def platform_stats(session: AsyncSession = Depends(get_session)):
    return await service.get_platform_stats(session)


@router.get("/stuck-transactions", response_model=list[StuckTransaction])
async def stuck_transactions(session: AsyncSession = Depends(get_session)):
    return await service.list_stuck_transactions(session)


@router.get("/audit-log", response_model=list[AuditLogEntry])
async def audit_log(session: AsyncSession = Depends(get_session)):
    return await service.list_audit_log(session)


@router.get("/users", response_model=list[AdminUserSummary])
async def list_users(q: str | None = None, session: AsyncSession = Depends(get_session)):
    return await service.search_users(session, q)


@router.get("/users/{user_id}", response_model=AdminUserDetail)
async def user_detail(user_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    return await service.get_user_detail(session, user_id)


@router.patch("/users/{user_id}/status", response_model=AdminUserSummary)
async def update_user_status(
    user_id: uuid.UUID,
    payload: AdminUserStatusUpdate,
    current_admin: User = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
):
    user = await service.set_user_active_status(session, user_id, payload.is_active)
    action = "reactivate_user" if payload.is_active else "suspend_user"
    await service.log_admin_action(session, current_admin.id, action, "user", user_id)
    return user


@router.patch("/users/{user_id}/kyc-tier", response_model=AdminUserSummary)
async def update_user_kyc_tier(
    user_id: uuid.UUID,
    payload: AdminKycTierUpdate,
    current_admin: User = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
):
    """Set a user's verification tier - the only way to reach ID_VERIFIED.
    Changes their transaction limits; audited with the reason given."""
    user, previous = await service.set_user_kyc_tier(session, user_id, payload.kyc_tier)
    await service.log_admin_action(
        session, current_admin.id, "set_kyc_tier", "user", user_id,
        details={"from": previous.value, "to": payload.kyc_tier.value, "reason": payload.reason},
    )
    return user


@router.post("/crossborder/transfers/{transfer_id}/retry", response_model=CrossBorderRead)
async def retry_crossborder_transfer(
    transfer_id: uuid.UUID,
    current_admin: User = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
):
    transfer = await service.get_crossborder_transfer(session, transfer_id)
    result = await crossborder_service.retry_delivery(session, transfer)
    await service.log_admin_action(
        session, current_admin.id, "retry_crossborder_transfer", "crossborder_transfer", transfer_id,
        details={"resulting_status": result.status.value},
    )
    return result


@router.post("/crossborder/transfers/{transfer_id}/refund", response_model=CrossBorderRead)
async def refund_crossborder_transfer(
    transfer_id: uuid.UUID,
    current_admin: User = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
):
    transfer = await service.get_crossborder_transfer(session, transfer_id)
    result = await crossborder_service.refund_delivery_failure(session, transfer)
    await service.log_admin_action(
        session, current_admin.id, "refund_crossborder_transfer", "crossborder_transfer", transfer_id,
        details={"amount": str(result.source_amount)},
    )
    return result


@router.post("/cards/{card_id}/retry", response_model=CardRead)
async def retry_card_creation(
    card_id: uuid.UUID,
    current_admin: User = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
):
    card = await service.get_card(session, card_id)
    result = await card_service.retry_card_creation(session, card)
    await service.log_admin_action(
        session, current_admin.id, "retry_card_creation", "card", card_id,
        details={"resulting_status": result.status.value},
    )
    return result


@router.post("/cards/{card_id}/refund", response_model=CardRead)
async def refund_card_creation(
    card_id: uuid.UUID,
    current_admin: User = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
):
    card = await service.get_card(session, card_id)
    result = await card_service.refund_card_creation(session, card)
    await service.log_admin_action(
        session, current_admin.id, "refund_card_creation", "card", card_id,
        details={"amount": str(result.initial_funding_ghs)},
    )
    return result
