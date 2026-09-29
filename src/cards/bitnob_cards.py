"""Bitnob Virtual Cards API calls - SANDBOX ONLY.

See src/common/bitnob_client.py for the shared auth/safety notes that
apply to every Bitnob integration in this codebase - same rule applies
here: no live-mode path, ever, without a deliberate decision made
outside this code.

Endpoints confirmed from https://bitnob.dev/api-reference/virtual-cards
and https://bitnob.dev/api-reference/customers:
  1. create_customer - POST /api/customers (prerequisite: card issuance
     needs a customer_id from this "Card KYC" step first)
  2. create_card      - POST /api/cards
  3. fund_card         - POST /api/cards/{card_id}/balance (type="fund")
  4. set_card_status   - POST /api/cards/{card_id}/status ("frozen"/"active")
  5. terminate_card    - DELETE /api/cards/{card_id} (blocked within 24h of creation)
  6. get_card          - GET /api/cards/{card_id}
  7. list_cards        - GET /api/cards

Amounts are documented as "micro-units" - assumed 1 unit = 1,000,000
micro-units (a common convention), NOT independently confirmed against
a real response the way the payouts amount format was. Verify this
against a real create_card/fund_card response before trusting it for
anything beyond a demo.
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


async def create_customer(email: str, first_name: str, last_name: str, customer_type: str = "individual") -> dict:
    """The cardholder name on a card is always taken from the customer
    record (first_name/last_name), never from the card-creation request
    itself - confirmed by a real 400 when a name was sent on create_card
    instead ("the customer record has no name... a name sent on this
    request is not used")."""
    body = {
        "email": email,
        "customer_type": customer_type,
        "first_name": first_name,
        "last_name": last_name,
    }
    return await request("POST", "/api/customers", body)


async def update_customer_kyc(
    customer_id: str,
    country: str,
    date_of_birth: str,  # YYYY-MM-DD
    id_type: str,
    id_number: str,
    dial_code: str,
    phone_number: str,
    line1: str,
    city: str,
    state: str,
    postal_code: str,
) -> dict:
    """Card issuance fails with "KYC verification required (status=none)"
    on a bare create_customer record - confirmed by a real 400. There's
    no dedicated KYC-submission endpoint documented; PUT on the customer
    with these fields is what the docs' field list for "customers
    created with full KYC" implies is needed, not independently
    confirmed as the exact mechanism that flips kyc_status - verify
    against a real response before relying on this for anything beyond
    a demo."""
    body = {
        "country": country,
        "date_of_birth": date_of_birth,
        "id_type": id_type,
        "id_number": id_number,
        "dial_code": dial_code,
        "phone_number": phone_number,
        "line1": line1,
        "city": city,
        "state": state,
        "postal_code": postal_code,
    }
    return await request("PUT", f"/api/customers/{customer_id}", body)


async def create_card(
    customer_id: str, name: str, amount: Decimal, currency: str = "USD", card_brand: str = "visa"
) -> dict:
    """Contradictory live behavior, both confirmed by real errors: an
    earlier attempt with no `name` field failed with "the customer
    record has no name... a name sent on this request is not used" -
    implying the customer's first_name/last_name should be enough. But
    with a properly-named customer, omitting `name` here instead failed
    with "Name is required" in a completely different error format. Both
    are sent now (customer first_name/last_name AND this field) since
    the live API's actual requirement doesn't match either error's
    advisory text taken alone."""
    body = {
        "customer_id": customer_id,
        "name": name,
        "amount": to_micro_units(amount),
        "currency": currency,
        "card_type": "virtual",
        "card_brand": card_brand,
    }
    return await request("POST", "/api/cards", body)


async def fund_card(card_id: str, amount: Decimal, reference: str) -> dict:
    body = {"amount": to_micro_units(amount), "type": "fund", "reference": reference}
    return await request("POST", f"/api/cards/{card_id}/balance", body)


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
