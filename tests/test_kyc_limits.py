"""KYC-tier transaction limits (common/kyc_limits.py) - restored 2026-09-30."""

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

from src.auth.models import KycTier, User
from src.common import kyc_limits


def user(tier: KycTier) -> User:
    return User(id=uuid.uuid4(), phone_number="+233200000009", full_name="Test User",
                hashed_password="x", referral_code=f"T{uuid.uuid4().hex[:6]}", kyc_tier=tier)


def test_tier_amounts_match_the_original():
    assert kyc_limits.TIER_LIMITS == {
        KycTier.UNVERIFIED: (Decimal("500"), Decimal("2000")),
        KycTier.PHONE_VERIFIED: (Decimal("5000"), Decimal("20000")),
        KycTier.ID_VERIFIED: (Decimal("20000"), Decimal("100000")),
    }


@pytest.fixture
def volumes(monkeypatch):
    """Stub the ledger: {'daily': x, 'monthly': y, 'in_flight': z}."""
    state = {"daily": Decimal("0"), "monthly": Decimal("0"), "in_flight": Decimal("0")}

    async def confirmed(session, user_id, since):
        from datetime import datetime, timezone
        hours = (datetime.now(timezone.utc) - since).total_seconds() / 3600
        return state["daily"] if hours <= 25 else state["monthly"]

    async def in_flight(session, user_id, since):
        return state["in_flight"]

    monkeypatch.setattr(kyc_limits, "_confirmed_volume_since", confirmed)
    monkeypatch.setattr(kyc_limits, "_in_flight_volume", in_flight)
    return state


async def test_within_limit_passes(volumes):
    volumes["daily"] = Decimal("400")
    await kyc_limits.check_transaction_limit(None, user(KycTier.UNVERIFIED), Decimal("100"))  # exactly 500


async def test_daily_limit_refused_with_next_step(volumes):
    volumes["daily"] = Decimal("400")
    with pytest.raises(HTTPException) as e:
        await kyc_limits.check_transaction_limit(None, user(KycTier.UNVERIFIED), Decimal("100.01"))
    assert e.value.status_code == 400
    assert "daily transaction limit of GHS 500" in e.value.detail and "Verify your phone number" in e.value.detail


async def test_monthly_limit_refused(volumes):
    volumes["monthly"] = Decimal("19990")
    with pytest.raises(HTTPException) as e:
        await kyc_limits.check_transaction_limit(None, user(KycTier.PHONE_VERIFIED), Decimal("20"))
    assert "monthly transaction limit of GHS 20000" in e.value.detail and "Verify your ID" in e.value.detail


async def test_unpaid_checkouts_count(volumes):
    # Three open checkouts of 1,500 each would each fit under 5,000 alone.
    volumes["in_flight"] = Decimal("4500")
    with pytest.raises(HTTPException):
        await kyc_limits.check_transaction_limit(None, user(KycTier.PHONE_VERIFIED), Decimal("1500"))


async def test_id_verified_gets_the_higher_limits(volumes):
    volumes["daily"] = Decimal("15000")
    await kyc_limits.check_transaction_limit(None, user(KycTier.ID_VERIFIED), Decimal("5000"))
    with pytest.raises(HTTPException):
        await kyc_limits.check_transaction_limit(None, user(KycTier.PHONE_VERIFIED), Decimal("5000"))


def test_record_skips_zero_and_adds_positive():
    added = []

    class FakeSession:
        def add(self, row):
            added.append(row)

    uid = uuid.uuid4()
    kyc_limits.record_transaction_volume(FakeSession(), uid, Decimal("0"), "wallet_transfer")
    kyc_limits.record_transaction_volume(FakeSession(), uid, Decimal("25.00"), "wallet_transfer")
    assert len(added) == 1 and added[0].amount == Decimal("25.00") and added[0].source == "wallet_transfer"
