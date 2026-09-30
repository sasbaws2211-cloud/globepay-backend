"""Bitnob's 3-strike decline rule (cards/decline_rule.py)."""

import uuid
from decimal import Decimal

import pytest

from src.cards import decline_rule as dr
from src.cards.models import VirtualCard


def card(strikes=0, balance="5.00"):
    return VirtualCard(user_id=uuid.uuid4(), initial_funding_ghs=Decimal("50"),
                       masked_pan="000000******3679", decline_strikes=strikes, balance=Decimal(balance))


@pytest.mark.parametrize("kind,reason,expected", [
    ("transaction.declined", "insufficient_funds", True),
    ("transaction.declined", "INSUFFICIENT_BALANCE", True),
    ("transaction.declined", "card_frozen", True),
    ("transaction.declined.frozen", None, True),
    ("transaction.declined", "invalid_cvv", False),            # user error, not a violation
    ("transaction.authorization.failed", "insufficient_funds", False),
    ("transaction.declined.terminated", "insufficient_funds", False),
])
def test_what_counts_as_a_violation(kind, reason, expected):
    assert dr.is_violation(kind, reason) is expected


def test_strikes_cap_at_three():
    c = card(strikes=2)
    dr.record_violation(c)
    dr.record_violation(c)
    assert c.decline_strikes == 3 and c.last_decline_at is not None


def test_bitnob_charge_count_wins_and_fee_recorded():
    c = card(strikes=0)
    fee = dr.record_charge(c, {"violationCount": 2, "feeAmount": 750000})  # micro-units
    assert c.decline_strikes == 2 and fee == Decimal("0.75") and c.decline_fees_usd == Decimal("0.75")
    dr.record_charge(c, {"violation_count": 3, "fee_amount": "0.75"})     # dollars, snake_case
    assert c.decline_strikes == 3 and c.decline_fees_usd == Decimal("1.50")


def test_charge_never_lowers_the_count():
    c = card(strikes=2)
    dr.record_charge(c, {"violationCount": 1, "feeAmount": 750000})
    assert c.decline_strikes == 2


def test_charge_without_fee_amount_uses_documented_fee():
    c = card()
    assert dr.record_charge(c, {"violationCount": 2}) == Decimal("0.75")


@pytest.mark.parametrize("strikes,expect", [
    (1, "1 of 3"), (2, "URGENT"), (3, "being closed"),
])
def test_strike_messages(strikes, expect):
    msg = dr.strike_message(card(strikes=strikes), "USD 9.99", " at NETFLIX")
    assert expect in msg and "3679" in msg and "NETFLIX" in msg


def test_strike_says_move_the_subscription():
    msg = dr.strike_message(card(strikes=1), "USD 9.99", " at NETFLIX")
    assert "can't be topped up" in msg and "full card" not in msg


def test_renewal_warning_only_when_balance_is_short():
    assert "declined" in dr.renewal_warning(card(balance="3.00"), Decimal("9.99"))
    assert dr.renewal_warning(card(balance="20.00"), Decimal("9.99")) == ""
    assert dr.renewal_warning(card(balance="3.00"), None) == ""
