"""Bitnob's decline rule, and keeping GlobePay users out of it.

From https://bitnob.dev/docs/card-issuing/card-decline-rule-policy: these
count as violations - a purchase declined for insufficient card balance, a
payment attempted on a frozen card, and an authorization the merchant never
finalises. 1st violation: free. 2nd: $0.75. 3rd: $0.75 and the card is
permanently terminated. Fees are charged to the company USD wallet
(virtualcard.transaction.declined.charge carries feeAmount + violationCount).
No reset window is documented.

A subscription renewing on an empty card walks straight into this, so each
strike gets a specific SMS saying where the card stands and what to do, and a
purchase that leaves too little for the same charge again says so. Fees are
passed on to the user (as with every Bitnob fee): recorded on the card and
taken from the leftover-balance payout when the card is closed.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from src.cards.models import VirtualCard

MAX_STRIKES = 3
STRIKE_FEE_USD = Decimal("0.75")

# Decline reasons that count toward the rule (Bitnob sends machine codes).
_VIOLATION_REASONS = ("insufficient", "frozen", "not_finalized", "not finalised", "not_finalised", "expired_authorization")


def is_violation(kind: str, reason: str | None) -> bool:
    if kind == "transaction.declined.frozen":
        return True
    if kind != "transaction.declined":
        return False  # e.g. authorization.failed (wrong CVV / AVS) doesn't count
    r = (reason or "").lower()
    return any(code in r for code in _VIOLATION_REASONS)


def _usd(value) -> Decimal | None:
    """feeAmount may arrive as micro-units (like every other card amount) or dollars."""
    if value in (None, ""):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return (d / 1_000_000).quantize(Decimal("0.01")) if d >= 1000 else d.quantize(Decimal("0.01"))


def _pick(d: dict, *keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def record_violation(card: VirtualCard) -> None:
    """A strike we saw as a decline. Bitnob's own count (declined.charge) wins
    when it arrives, so this only ever moves the count forward."""
    card.decline_strikes = min(card.decline_strikes + 1, MAX_STRIKES)
    card.last_decline_at = datetime.now(timezone.utc)


def record_charge(card: VirtualCard, data: dict) -> Decimal | None:
    """virtualcard.transaction.declined.charge: Bitnob charged a violation fee.
    Returns the fee (USD) if one was given."""
    count = _pick(data, "violationCount", "violation_count")
    try:
        if count is not None:
            card.decline_strikes = min(max(card.decline_strikes, int(count)), MAX_STRIKES)
    except (TypeError, ValueError):
        pass
    fee = _usd(_pick(data, "feeAmount", "fee_amount"))
    if fee is None and count is not None:
        fee = STRIKE_FEE_USD  # documented fee, if the payload didn't carry one
    if fee:
        card.decline_fees_usd = (card.decline_fees_usd or Decimal("0")) + fee
    card.last_decline_at = datetime.now(timezone.utc)
    return fee


def strike_message(card: VirtualCard, amount_text: str, merchant_text: str) -> str:
    """SMS after a violation - where the card stands and what to do next."""
    last4 = (card.masked_pan or "")[-4:] or "card"
    n = card.decline_strikes
    fix = "This card can't be topped up - cancel the subscription or move it to a new card"
    if n >= MAX_STRIKES:
        return (f"GlobePay card ending {last4}: {amount_text}{merchant_text} was declined - that's the 3rd, "
                f"so the card is being closed. Any balance left, less decline fees, will be sent to your mobile money.")
    if n == MAX_STRIKES - 1:
        return (f"URGENT - GlobePay card ending {last4}: {amount_text}{merchant_text} was declined (2 of 3). "
                f"One more declined payment and the card is closed for good. {fix}.")
    return (f"GlobePay card ending {last4}: {amount_text}{merchant_text} was declined - not enough balance. "
            f"That's 1 of 3: after 3 declined payments the card is closed, and the 2nd and 3rd cost "
            f"${STRIKE_FEE_USD} each. {fix}.")


def renewal_warning(card: VirtualCard, spent_usd: Decimal | None) -> str:
    """Appended to a purchase SMS when the balance couldn't cover the same
    charge again - i.e. the next renewal of this subscription would decline."""
    if spent_usd is None or spent_usd <= 0 or card.balance >= spent_usd:
        return ""
    return " That's more than what's left - if this renews, it will be declined. Move it to a new card before it renews."
