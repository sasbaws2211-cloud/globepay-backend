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


def test_tier_amounts():
    assert kyc_limits.TIER_LIMITS == {
        KycTier.UNVERIFIED: (Decimal("500"), Decimal("2000")),
        # Paystack's max single transfer in Ghana; Paystack has no monthly cap.
        KycTier.PHONE_VERIFIED: (Decimal("50000"), None),
        KycTier.ID_VERIFIED: (None, None),
    }


@pytest.fixture
def volumes(monkeypatch):
    """Stub the ledger: {'daily': x, 'monthly': y, 'in_flight': z}. Limits on."""
    monkeypatch.setattr(kyc_limits.settings, "ENFORCE_TRANSACTION_LIMITS", True)
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
    volumes["monthly"] = Decimal("1990")
    with pytest.raises(HTTPException) as e:
        await kyc_limits.check_transaction_limit(None, user(KycTier.UNVERIFIED), Decimal("20"))
    assert "monthly transaction limit of GHS 2000" in e.value.detail and "Verify your phone number" in e.value.detail


async def test_phone_verified_daily_is_paystacks_50000(volumes):
    volumes["daily"] = Decimal("45000")
    await kyc_limits.check_transaction_limit(None, user(KycTier.PHONE_VERIFIED), Decimal("5000"))  # exactly 50,000
    with pytest.raises(HTTPException) as e:
        await kyc_limits.check_transaction_limit(None, user(KycTier.PHONE_VERIFIED), Decimal("5000.01"))
    assert "daily transaction limit of GHS 50000" in e.value.detail and "Verify your ID" in e.value.detail


async def test_phone_verified_has_no_monthly_limit(volumes):
    volumes["monthly"] = Decimal("5000000")  # far past any monthly figure
    await kyc_limits.check_transaction_limit(None, user(KycTier.PHONE_VERIFIED), Decimal("1000"))


async def test_unpaid_checkouts_count(volumes):
    # Open checkouts that each fit under 50,000 alone still add up.
    volumes["in_flight"] = Decimal("49000")
    with pytest.raises(HTTPException):
        await kyc_limits.check_transaction_limit(None, user(KycTier.PHONE_VERIFIED), Decimal("1500"))


async def test_id_verified_has_no_limits(volumes):
    volumes["daily"] = volumes["monthly"] = Decimal("10000000")
    await kyc_limits.check_transaction_limit(None, user(KycTier.ID_VERIFIED), Decimal("1000000"))


async def test_switched_off_refuses_nothing(volumes, monkeypatch):
    monkeypatch.setattr(kyc_limits.settings, "ENFORCE_TRANSACTION_LIMITS", False)
    volumes["daily"] = volumes["monthly"] = Decimal("999999")
    await kyc_limits.check_transaction_limit(None, user(KycTier.UNVERIFIED), Decimal("1000000"))


def test_limits_are_on_by_default():
    from src.config import Settings
    assert Settings.model_fields["ENFORCE_TRANSACTION_LIMITS"].default is True


def test_record_skips_zero_and_adds_positive():
    added = []

    class FakeSession:
        def add(self, row):
            added.append(row)

    uid = uuid.uuid4()
    kyc_limits.record_transaction_volume(FakeSession(), uid, Decimal("0"), "wallet_transfer")
    kyc_limits.record_transaction_volume(FakeSession(), uid, Decimal("25.00"), "wallet_transfer")
    assert len(added) == 1 and added[0].amount == Decimal("25.00") and added[0].source == "wallet_transfer"
