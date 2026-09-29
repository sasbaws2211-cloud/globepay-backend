"""Thin wrapper around the Paystack REST API.

Used for both collecting contributions (Initialize Transaction, which
supports mobile money charge in Ghana) and paying out (Transfer Recipient +
Transfer). No funds are ever held by this platform: every inbound
contribution and every outbound payout is a real Paystack transaction.
"""

from decimal import Decimal
from typing import Any

import httpx

from src.config import settings


class PaystackError(Exception):
    def __init__(self, message: str, response_body: Any = None):
        super().__init__(message)
        self.response_body = response_body


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }


def _to_subunit(amount: Decimal) -> int:
    """Paystack amounts are in the currency's smallest unit (pesewas for GHS)."""
    return int(amount * 100)


def to_local_momo_number(number: str) -> str:
    """Paystack Ghana only accepts mobile_money account numbers in local
    0XXXXXXXXX form - "+233240000002" is rejected with "Account number is
    invalid" - while users (and the seed data) store numbers in +233 form.
    Normalize +233/233/bare 9-digit forms; anything unrecognized is passed
    through unchanged so Paystack's own validation message still surfaces."""
    digits = "".join(ch for ch in number if ch.isdigit())
    if len(digits) == 12 and digits.startswith("233"):
        return "0" + digits[3:]
    if len(digits) == 9:
        return "0" + digits
    if len(digits) == 10 and digits.startswith("0"):
        return digits
    return number.strip()


async def initialize_transaction(
    email: str, amount: Decimal, reference: str, metadata: dict | None = None
) -> dict:
    payload = {
        "email": email,
        "amount": _to_subunit(amount),
        "currency": "GHS",
        "reference": reference,
        "metadata": metadata or {},
    }
    async with httpx.AsyncClient(base_url=settings.PAYSTACK_BASE_URL) as client:
        resp = await client.post("/transaction/initialize", json=payload, headers=_headers())
    body = resp.json()
    if not body.get("status"):
        raise PaystackError(body.get("message", "Failed to initialize transaction"), body)
    return body["data"]


async def verify_transaction(reference: str) -> dict:
    async with httpx.AsyncClient(base_url=settings.PAYSTACK_BASE_URL) as client:
        resp = await client.get(f"/transaction/verify/{reference}", headers=_headers())
    body = resp.json()
    if not body.get("status"):
        raise PaystackError(body.get("message", "Failed to verify transaction"), body)
    return body["data"]


async def create_transfer_recipient(
    name: str, account_number: str, bank_code: str, currency: str = "GHS"
) -> str:
    """account_number is the MoMo number for mobile_money recipients; bank_code
    identifies the network (e.g. MTN/Vodafone/AirtelTigo) per Paystack Ghana's
    bank-code list — confirm exact codes against their Ghana docs before use."""
    payload = {
        "type": "mobile_money",
        "name": name,
        "account_number": to_local_momo_number(account_number),
        "bank_code": bank_code,
        "currency": currency,
    }
    async with httpx.AsyncClient(base_url=settings.PAYSTACK_BASE_URL) as client:
        resp = await client.post("/transferrecipient", json=payload, headers=_headers())
    body = resp.json()
    if not body.get("status"):
        raise PaystackError(body.get("message", "Failed to create transfer recipient"), body)
    return body["data"]["recipient_code"]


async def initiate_transfer(amount: Decimal, recipient_code: str, reason: str, reference: str) -> dict:
    payload = {
        "source": "balance",
        "amount": _to_subunit(amount),
        "recipient": recipient_code,
        "reason": reason,
        "reference": reference,
    }
    async with httpx.AsyncClient(base_url=settings.PAYSTACK_BASE_URL) as client:
        resp = await client.post("/transfer", json=payload, headers=_headers())
    body = resp.json()
    if not body.get("status"):
        raise PaystackError(body.get("message", "Failed to initiate transfer"), body)
    return body["data"]


async def verify_transfer(reference: str) -> dict:
    """Current state of an outbound payout. data.status is one of
    pending / queued / processing / otp / success / failed / reversed -
    used to reconcile payouts whose transfer.* webhook never arrived."""
    async with httpx.AsyncClient(base_url=settings.PAYSTACK_BASE_URL) as client:
        resp = await client.get(f"/transfer/verify/{reference}", headers=_headers())
    body = resp.json()
    if not body.get("status"):
        raise PaystackError(body.get("message", "Failed to verify transfer"), body)
    return body["data"]


# Paystack payout statuses that are final, mapped to the webhook event the
# transfer.* handlers already understand.
TRANSFER_FINAL_EVENTS = {"success": "transfer.success", "failed": "transfer.failed", "reversed": "transfer.reversed"}


async def charge_authorization(authorization_code: str, email: str, amount: Decimal, reference: str) -> dict:
    """Charge a card the customer has already paid with once before, using
    the reusable authorization_code Paystack returns on that first charge's
    verify response - no checkout redirect, no OTP in the common case
    (Paystack may still occasionally require one depending on the issuing
    bank). This is what makes recurring/scheduled savings possible: the
    response's `data.status` is synchronous like verify_transaction's, not
    a bare acknowledgement like refund_transaction's."""
    payload = {
        "authorization_code": authorization_code,
        "email": email,
        "amount": _to_subunit(amount),
        "reference": reference,
    }
    async with httpx.AsyncClient(base_url=settings.PAYSTACK_BASE_URL) as client:
        resp = await client.post("/transaction/charge_authorization", json=payload, headers=_headers())
    body = resp.json()
    if not body.get("status"):
        raise PaystackError(body.get("message", "Failed to charge saved authorization"), body)
    return body["data"]


async def refund_transaction(reference: str, amount: Decimal | None = None) -> dict:
    """Refund a previously successful charge, identified by its original
    reference. Paystack processes refunds asynchronously - this response is
    just an acknowledgement that the refund was accepted, not proof the
    money has landed back with the customer yet. Omitting `amount` refunds
    the full original charge."""
    payload: dict[str, Any] = {"transaction": reference}
    if amount is not None:
        payload["amount"] = _to_subunit(amount)
    async with httpx.AsyncClient(base_url=settings.PAYSTACK_BASE_URL) as client:
        resp = await client.post("/refund", json=payload, headers=_headers())
    body = resp.json()
    if not body.get("status"):
        raise PaystackError(body.get("message", "Failed to initiate refund"), body)
    return body["data"]


async def fetch_refund(refund_id: str) -> dict:
    """GET /refund/{id} - the refund's current state (confirmed live: a fresh
    refund reads "pending"). Paystack statuses: pending / processing /
    processed / failed (plus needs-attention, which isn't final)."""
    async with httpx.AsyncClient(base_url=settings.PAYSTACK_BASE_URL) as client:
        resp = await client.get(f"/refund/{refund_id}", headers=_headers())
    body = resp.json()
    if not body.get("status"):
        raise PaystackError(body.get("message", "Failed to fetch refund"), body)
    return body["data"]


# Final refund states; anything else means "still in progress, check again later".
REFUND_FINAL_STATUSES = ("processed", "failed")
