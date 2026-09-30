"""Lite card loads: Bitnob's limits and fees, passed on to the user (service.price_load)."""

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

from src.cards import service
from src.cards.models import VirtualCard

RATE = Decimal("15.5")  # settings.DEMO_GHS_USD_RATE


def price(amount):
    return service.price_load(Decimal(amount))


def test_new_card_pays_creation_and_funding_fee():
    q = price("155")  # $10 on the card
    assert q["amount_usd"] == Decimal("10.00")
    assert q["fee_usd"] == Decimal("2")  # $1 creation + $1 funding fee (confirmed live)
    assert q["fee_ghs"] == Decimal("31.00") and q["total_ghs"] == Decimal("186.00")


def test_funding_fee_is_one_percent_from_100_dollars():
    assert price("3100")["fee_usd"] == Decimal("3.00")    # $200 -> 1% ($2) + $1 creation
    assert price("1534.5")["fee_usd"] == Decimal("2")     # $99 -> flat $1 + $1 creation


@pytest.mark.parametrize("amount", [
    "30",    # under $2
    "3891",  # over the $250 lite cap
])
def test_limits_refused(amount):
    with pytest.raises(HTTPException) as exc:
        price(amount)
    assert exc.value.status_code == 400


def test_max_lite_load_accepted():
    assert price("3875")["amount_usd"] == Decimal("250.00")


def test_limits_endpoint_is_lite_only():
    limits = service.get_card_limits()
    assert limits["card_type"] == "lite" and limits["can_top_up"] is False
    assert "standard" not in limits
    assert limits["max_load_usd"] == Decimal("250")


def test_refund_covers_the_fee_too():
    card = VirtualCard(user_id=uuid.uuid4(), initial_funding_ghs=Decimal("155"), fee_ghs=Decimal("31"))
    assert card.charged_ghs == Decimal("186")
