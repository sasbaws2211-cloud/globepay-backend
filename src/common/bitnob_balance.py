"""The company's Bitnob balance (the float every payout and card is paid from),
read in a way that survives Bitnob's post-debit zero.

Seen live three times on 2026-09-29: right after money leaves the account,
GET /api/balances reports 0.0 - ledger AND available - for about 4-7 minutes,
then the correct figure again (20.25 -> 0 -> 14.25 after a card was created,
funded and withdrawn from; 1.54 -> 0 -> 1.54 after a payout). Taken at face
value, every pre-check in that window refuses ("temporarily unavailable")
even though the money is there.

So a 0 is only believed when nothing suggests the glitch:
  1. A non-zero reading is trusted and becomes the baseline.
  2. A 0 within TRUST_WINDOW of a good reading -> that reading minus what this
     app has spent since (record_debit, called wherever we move Bitnob money).
  3. A 0 with no recent baseline (e.g. just after a restart) -> if Bitnob's
     transaction list shows money moving in the last RECENT_ACTIVITY window,
     the balance is unknown (None - callers treat that as "can't verify");
     otherwise it's a real 0.

The baseline lives in memory: right for a single instance (the current
deployment); with several instances each keeps its own, and rule 3 still
covers an instance that has none.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from src.common.bitnob_client import request

logger = logging.getLogger(__name__)

MICRO = Decimal(1_000_000)
TRUST_WINDOW_SECONDS = 15 * 60  # the glitch has lasted up to ~7 minutes
RECENT_ACTIVITY = timedelta(minutes=10)

_baseline: dict[str, tuple[float, Decimal]] = {}  # asset -> (monotonic time, last good reading)
_debits: dict[str, list[tuple[float, Decimal]]] = {}  # asset -> spends recorded since the baseline


def record_debit(asset: str, amount: Decimal) -> None:
    """Call right after this app moves money out of the Bitnob balance (payout
    finalized, card created or funded) so a 0 reading straight afterwards can
    be answered from the baseline. Over-estimating fees is the safe side."""
    if amount > 0:
        _debits.setdefault(asset, []).append((time.monotonic(), amount))


async def _read(asset: str) -> Decimal | None:
    data = (await request("GET", "/api/balances")).get("data") or {}
    for account in data.get("accounts") or []:
        if account.get("currency") == asset:
            try:
                return Decimal(str(account.get("available_balance", "0"))) / MICRO
            except InvalidOperation:
                return None
    return None


async def _recent_activity(asset: str) -> bool:
    """Did money move on the account in the last RECENT_ACTIVITY? Fails
    towards True: if we can't tell, don't claim a hard 0."""
    try:
        data = (await request("GET", "/api/transactions?limit=5")).get("data") or {}
    except Exception:  # BitnobError or anything unexpected - we just can't tell
        return True
    cutoff = datetime.now(timezone.utc) - RECENT_ACTIVITY
    for tx in (data.get("transactions") if isinstance(data, dict) else data) or []:
        if tx.get("currency") not in (None, asset):
            continue
        try:
            if datetime.fromisoformat(str(tx.get("created_at")).replace("Z", "+00:00")) >= cutoff:
                return True
        except ValueError:
            continue
    return False


async def available_balance(asset: str) -> Decimal | None:
    """Spendable balance in `asset` (e.g. USDC), or None if it can't be
    determined right now. Raises BitnobError if Bitnob can't be reached."""
    reading = await _read(asset)
    if reading is None:
        return None
    now = time.monotonic()
    if reading > 0:
        _baseline[asset] = (now, reading)
        _debits[asset] = []
        return reading

    baseline = _baseline.get(asset)
    if baseline and now - baseline[0] < TRUST_WINDOW_SECONDS:
        spent = sum((amount for at, amount in _debits.get(asset, []) if at >= baseline[0]), Decimal("0"))
        estimate = max(baseline[1] - spent, Decimal("0"))
        logger.info("Bitnob reports 0 %s; using last good reading %s minus %s spent since = %s",
                    asset, baseline[1], spent, estimate)
        return estimate

    if await _recent_activity(asset):
        logger.warning("Bitnob reports 0 %s right after account activity and there's no recent good "
                       "reading - treating the balance as unknown", asset)
        return None
    return Decimal("0")
