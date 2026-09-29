"""In-process background scheduler (APScheduler).

This app runs as a single process with no message broker, so an
AsyncIOScheduler sharing FastAPI's own event loop is the right amount of
infrastructure for a pitch-stage product - not Celery/RQ plus a Redis
instance to operate. The tradeoff, worth knowing before this ever scales
past one instance: running two copies of this process means the recurring
charge sweep runs twice as often. A multi-instance deployment should move
this to a proper worker with its own single scheduling source of truth.
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from src.db.main import engine
from src.payments.reconcile import sweep_in_flight_payments
from src.vaults.service import sweep_recurring_charges

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def _run_recurring_charge_sweep() -> None:
    async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
        try:
            count = await sweep_recurring_charges(session)
            if count:
                logger.info("Recurring charge sweep: processed %d vault(s)", count)
        except Exception:
            logger.exception("Recurring charge sweep failed")


async def _run_payment_reconcile_sweep() -> None:
    async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
        try:
            count = await sweep_in_flight_payments(session)
            if count:
                logger.info("Payment reconcile sweep: %d item(s) updated", count)
        except Exception:
            logger.exception("Payment reconcile sweep failed")


def start() -> None:
    scheduler.add_job(
        _run_recurring_charge_sweep,
        "interval",
        hours=1,
        id="recurring_charge_sweep",
        replace_existing=True,
    )
    scheduler.add_job(
        _run_payment_reconcile_sweep,
        "interval",
        minutes=2,
        id="payment_reconcile_sweep",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()


def shutdown() -> None:
    scheduler.shutdown(wait=False)
