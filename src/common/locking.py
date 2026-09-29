"""Row-lock helper for payment state transitions.

A charge can now be confirmed from three places at once - the Paystack
webhook, the app polling /payments/{reference}/refresh, and the background
reconcile sweep. Every confirm step does "check status, then credit/deliver",
so without a lock two of them can both pass the check and credit a vault
twice or create a card twice.
"""

from typing import TypeVar

from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

M = TypeVar("M", bound=SQLModel)


async def locked_first(session: AsyncSession, model: type[M], *where) -> M | None:
    """SELECT ... FOR UPDATE: a concurrent caller waits until this
    transaction commits, then sees the new status. populate_existing forces a
    re-read even if the row is already in the session's identity map -
    otherwise the lock would be taken but stale in-memory state returned."""
    result = await session.exec(
        select(model).where(*where).with_for_update().execution_options(populate_existing=True)
    )
    return result.first()
