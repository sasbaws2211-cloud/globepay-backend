import uuid

from fastapi import APIRouter, Depends, Header, Response, status
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.dependencies import get_current_user
from src.auth.models import User
from src.common.idempotency import run_idempotently
from src.db.main import get_session
from src.vaults import service
from src.vaults.schemas import (
    ContributionInitiate,
    ContributionInitiateResponse,
    VaultCreate,
    VaultRead,
    WithdrawalRead,
    WithdrawalRequest,
)

router = APIRouter(prefix="/vaults", tags=["vaults"])


@router.post("", response_model=VaultRead, status_code=status.HTTP_201_CREATED)
async def create_vault(
    data: VaultCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    return await service.create_vault(session, current_user.id, data)


@router.get("", response_model=list[VaultRead])
async def list_vaults(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    return await service.list_user_vaults_read(session, current_user.id)


@router.get("/{vault_id}", response_model=VaultRead)
async def get_vault(
    vault_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    return await service.get_owned_vault(session, vault_id, current_user.id)


@router.post("/{vault_id}/cancel", response_model=VaultRead)
async def cancel_vault(
    vault_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    vault = await service.get_owned_vault(session, vault_id, current_user.id)
    return await service.cancel_vault(session, vault)


@router.post("/{vault_id}/contribute", response_model=ContributionInitiateResponse)
async def contribute(
    vault_id: uuid.UUID,
    payload: ContributionInitiate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    vault = await service.get_owned_vault(session, vault_id, current_user.id)

    async def _handler() -> dict:
        return await service.initiate_contribution(session, vault, payload.amount, payload.email)

    if idempotency_key:
        return await run_idempotently(
            session, current_user.id, idempotency_key, f"POST /vaults/{vault_id}/contribute",
            payload.model_dump(mode="json"), _handler,
        )
    return await _handler()


@router.post("/{vault_id}/withdraw", response_model=WithdrawalRead)
async def withdraw(
    vault_id: uuid.UUID,
    payload: WithdrawalRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    vault = await service.get_owned_vault(session, vault_id, current_user.id)
    return await service.request_withdrawal(session, vault, payload)


@router.post("/{vault_id}/recurring/enable", response_model=VaultRead)
async def enable_recurring(
    vault_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    vault = await service.get_owned_vault(session, vault_id, current_user.id)
    return await service.enable_recurring(session, vault, current_user)


@router.post("/{vault_id}/recurring/pause", response_model=VaultRead)
async def pause_recurring(
    vault_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    vault = await service.get_owned_vault(session, vault_id, current_user.id)
    return await service.pause_recurring(session, vault)


@router.post("/{vault_id}/recurring/resume", response_model=VaultRead)
async def resume_recurring(
    vault_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    vault = await service.get_owned_vault(session, vault_id, current_user.id)
    return await service.resume_recurring(session, vault)


@router.post("/{vault_id}/recurring/cancel", response_model=VaultRead)
async def cancel_recurring(
    vault_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    vault = await service.get_owned_vault(session, vault_id, current_user.id)
    return await service.cancel_recurring(session, vault)


@router.get("/{vault_id}/statement")
async def vault_statement(
    vault_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    vault = await service.get_owned_vault(session, vault_id, current_user.id)
    csv_text = await service.generate_vault_statement_csv(session, vault)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{vault.name}-statement.csv"'},
    )
