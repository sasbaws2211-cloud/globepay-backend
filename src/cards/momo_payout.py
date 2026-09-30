"""Paying card money out to the owner's mobile money via a Paystack transfer.

Used for a terminated card's leftover balance (termination_payout.py): Bitnob
has already moved the dollars into the company wallet, and GlobePay sends the
GHS equivalent.
"""

import logging
from decimal import Decimal

from src.auth.models import User
from src.config import settings
from src.payments import paystack

logger = logging.getLogger(__name__)


def usd_to_ghs(amount_usd: Decimal) -> Decimal:
    # Same fixed demo rate cards are loaded at (cards/service.py).
    return (amount_usd * Decimal(str(settings.DEMO_GHS_USD_RATE))).quantize(Decimal("0.01"))


async def send(owner: User, amount_ghs: Decimal, reason: str, reference: str) -> str:
    """Request the transfer; returns Paystack's reference. Raises PaystackError
    if there's no payout number or Paystack refuses the request. The outcome
    arrives later (transfer.* webhook, or the reconcile sweep)."""
    if not owner.default_momo_number or not owner.default_momo_bank_code:
        raise paystack.PaystackError("No mobile money payout number is set in Settings")
    recipient_code = await paystack.create_transfer_recipient(
        name=owner.default_account_name or owner.full_name,
        account_number=owner.default_momo_number,
        bank_code=owner.default_momo_bank_code,
    )
    transfer = await paystack.initiate_transfer(
        amount=amount_ghs, recipient_code=recipient_code, reason=reason, reference=reference
    )
    if transfer.get("status") == "otp":
        logger.warning("Card payout %s is held for OTP in the Paystack dashboard", reference)
    return transfer.get("reference", reference)
