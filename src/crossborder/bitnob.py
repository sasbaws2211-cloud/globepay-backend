"""Bitnob Payouts (Offramp) API calls - SANDBOX ONLY.

See src/common/bitnob_client.py for the shared auth/safety notes that
apply to every Bitnob integration in this codebase.

Payout flow is three real API calls, confirmed from
https://bitnob.dev/api-reference/payouts (an earlier attempt guessed a
two-step flow with a wrong /v1/-prefixed path and camelCase fields from
an unverified web search result - both were wrong, caught only by
testing against a real key and getting a live 404/403/401 at each
wrong turn, then reading the actual reference page):
  1. create_quote      - POST /api/payouts/quotes
  2. initialize_payout - POST /api/payouts/{quote_id}/initialize
  3. finalize_payout   - POST /api/payouts/{quote_id}/finalize (empty body)
Initialize must happen before the quote's expires_at. finalize returns
PENDING immediately and settles to SUCCESS/FAILED asynchronously
(confirmed ~150ms in sandbox testing) - get_payout (using the payout's
internal UUID `id`, not the `quote_id` string used everywhere else) is
needed to observe the real outcome.
"""

from decimal import Decimal

from src.common import bitnob_balance
from src.common.bitnob_client import BitnobError, request  # noqa: F401 - re-exported for callers

BASE_URL = "https://api.bitnob.com"  # kept for backwards-compat readability; see bitnob_client for the real one


async def create_quote(
    from_asset: str, to_currency: str, country: str, amount: str, reference: str, source: str = "offchain", chain: str | None = None
) -> dict:
    """amount is a decimal string in `from_asset` units (e.g. "20.00" USDC),
    not the lowest-denomination integer an earlier wrong assumption used.

    source="offchain" debits a Bitnob account balance directly and skips
    the on-chain-deposit step entirely - source="onchain" instead returns
    a deposit address and stays PENDING_ADDRESS_DEPOSIT until real crypto
    actually arrives there, confirmed not simulated in sandbox. offchain
    is what makes this usable as a demo without real wallet/crypto
    management; a live version of this feature might use onchain instead."""
    body: dict = {
        "amount": amount,
        "country": country,
        "from_asset": from_asset,
        "to_currency": to_currency,
        "source": source,
        "reference": reference,
    }
    if chain is not None:
        body["chain"] = chain
    return await request("POST", "/api/payouts/quotes", body)


async def initialize_payout(quote_id: str, reference: str, payment_reason: str, beneficiary: dict) -> dict:
    body = {
        "quote_id": quote_id,
        "reference": reference,
        "payment_reason": payment_reason,
        "beneficiary": beneficiary,
    }
    return await request("POST", f"/api/payouts/{quote_id}/initialize", body)


async def finalize_payout(quote_id: str) -> dict:
    return await request("POST", f"/api/payouts/{quote_id}/finalize")


async def available_balance(asset: str) -> Decimal | None:
    """The company account's spendable balance in `asset` (e.g. USDC) - the
    float every offchain payout is debited from. None if it can't be told
    right now: Bitnob reports 0 for minutes after every debit, see
    src/common/bitnob_balance.py."""
    return await bitnob_balance.available_balance(asset)


async def get_payout(bitnob_id: str) -> dict:
    """Needs the payout's internal UUID `id`, not the `quote_id` string
    used everywhere else - confirmed by a real 400 ("invalid UUID") when
    quote_id was tried here first."""
    return await request("GET", f"/api/payouts/{bitnob_id}")
