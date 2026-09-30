"""Bitnob Virtual Cards API calls - SANDBOX ONLY.

See src/common/bitnob_client.py for the shared auth/safety notes that
apply to every Bitnob integration in this codebase - same rule applies
here: no live-mode path, ever, without a deliberate decision made
outside this code.

GlobePay issues LITE cards only - no Card KYC, loaded once at creation,
never topped up or withdrawn from. Endpoints used, from
https://bitnob.dev/api-reference/virtual-cards and
https://bitnob.dev/api-reference/customers:
  1. create_lite_card      - POST /api/cards/lite (creates the customer too)
  2. find_customer_by_phone - GET /api/customers?phone_number= (card cap)
  3. set_card_status       - POST /api/cards/{card_id}/status ("frozen"/"active")
  4. terminate_card        - DELETE /api/cards/{card_id} (blocked within 24h of creation)
  5. get_card / get_card_secure / list_transactions

Amounts are "micro-units": 1 USD = 1,000,000 (confirmed on real card
responses and transaction lists).
"""

from decimal import Decimal

from src.common.bitnob_client import BitnobError, request  # noqa: F401 - re-exported for callers

MICRO_UNITS_PER_UNIT = 1_000_000

# Lite cards - the only card type this app issues (create_lite_card). From
# https://bitnob.dev/api-reference/virtual-cards: "you can only spend and
# terminate a lite card - funding is not supported", with a $250 cap on the
# one-time load at creation. Confirmed live 2026-09-29: top-ups fail with
# "topups are not supported for lite cards".
LITE_MAX_LOAD_USD = 250
# Not in Bitnob's docs - observed live in sandbox ("customer already has 3
# active cards, maximum is 3"). Bitnob matches a lite-card customer by phone
# number, not email, so the cap is per phone number.
MAX_LITE_CARDS_PER_CUSTOMER = 3
# Bitnob's published fee schedule: $1.00 per card created, charged to the
# company wallet (the card itself receives the full load - confirmed on a
# real sandbox card's transactions).
CARD_CREATION_FEE_USD = 1
# Funding fee, charged on a lite card's one load at creation (confirmed live
# 2026-09-29/30 against the balance and the card's own transaction list):
# $1 under $100, else 1%.
FUNDING_FEE_FLAT_USD = 1
FUNDING_FEE_FLAT_BELOW_USD = 100
FUNDING_FEE_PERCENT = 1


def to_micro_units(amount: Decimal) -> int:
    return int(amount * MICRO_UNITS_PER_UNIT)


def from_micro_units(micro: int) -> Decimal:
    return (Decimal(micro) / MICRO_UNITS_PER_UNIT).quantize(Decimal("0.01"))


async def create_lite_card(
    first_name: str,
    last_name: str,
    email: str,
    phone_number: str,
    dial_code: str,
    amount: Decimal,
    currency: str = "USD",
) -> dict:
    """Bypasses the full async Card KYC flow entirely - confirmed from
    https://bitnob.dev/api-reference/virtual-cards#overview after the
    full flow (create_customer -> update_customer_kyc -> create_card)
    got stuck on kyc_status never leaving "" even with complete
    demographic data submitted. Creates the customer AND the card in one
    call from just basic contact details (Bitnob reuses an existing
    customer with the same phone number). Per the current docs a lite card
    CAN be spent with and terminated, but never funded again - `amount`
    here (max LITE_MAX_LOAD_USD) is the only money it will ever receive."""
    body = {
        "type": "lite",
        "amount": to_micro_units(amount),
        "currency": currency,
        "name": f"{first_name} {last_name}",
        "customer": {
            "customer_type": "individual",
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "phone_number": phone_number,
            "dial_code": dial_code,
        },
    }
    return await request("POST", "/api/cards/lite", body)


async def find_customer_by_phone(phone_number: str) -> dict | None:
    """GET /api/customers?phone_number= - confirmed live to filter exactly.
    The customer record carries card_counts ({active, frozen, pending, ...}),
    which is what the per-customer card cap is checked against."""
    data = (await request("GET", f"/api/customers?phone_number={phone_number}")).get("data") or {}
    customers = data.get("customers") or []
    return customers[0] if customers else None


async def set_card_status(card_id: str, status: str) -> dict:
    """status: 'frozen' or 'active'."""
    return await request("POST", f"/api/cards/{card_id}/status", {"status": status})


async def terminate_card(card_id: str, reason: str) -> dict:
    """Bitnob blocks termination within 24h of card creation - raises
    BitnobError in that case, handle it as a normal user-facing error,
    not a crash."""
    return await request("DELETE", f"/api/cards/{card_id}", {"reason": reason})


async def get_card(card_id: str) -> dict:
    return await request("GET", f"/api/cards/{card_id}")


async def get_card_secure(card_id: str) -> dict:
    """GET /api/cards/{card_id}/secure - full number/CVV/expiry, encrypted to
    the public key registered in Bitnob's dashboard (see cards/secure_details.py).
    Docs say 400 "no encryption key is registered" until that's done; the
    sandbox actually returns them unencrypted under data.details instead."""
    return await request("GET", f"/api/cards/{card_id}/secure")


async def simulate_transaction(card_id: str, event_type: str) -> dict:
    """Sandbox-only (enforced by Bitnob middleware, not by anything on our
    side): fires a simulated card transaction event so the actual "can this
    card spend" question can be tested directly rather than inferred from
    a documentation note ("spending is not supported") that contradicts
    the spend-tracking fields (spent_current_month) a real card response
    includes. event_type: 'authorization', 'settlement', 'reversal', or
    'decline'. Confirmed from
    https://bitnob.dev/api-reference/virtual-cards/simulate-webhook."""
    return await request("POST", f"/api/cards/{card_id}/simulate-webhook", {"card_id": card_id, "event_type": event_type})


async def list_transactions(card_id: str, page: int = 1, limit: int = 50) -> dict:
    """List spend/credit activity for a card from Bitnob.
    Confirmed under Virtual Cards "Card Transactions" in Bitnob docs.
    Returns the raw provider payload; callers normalize fields.
    """
    return await request("GET", f"/api/cards/{card_id}/transactions?page={page}&limit={limit}")
