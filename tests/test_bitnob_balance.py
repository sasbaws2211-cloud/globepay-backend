"""Bitnob reports a 0 balance for minutes after every debit (seen live
2026-09-29); src/common/bitnob_balance.py must not believe it blindly."""

from decimal import Decimal

import pytest

from src.common import bitnob_balance as bb


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def fake(monkeypatch):
    """Scripted Bitnob: set .reading / .activity per step; controllable clock."""
    state = type("S", (), {"reading": Decimal("0"), "activity": False, "activity_calls": 0})()
    clock = Clock()

    async def read(asset):
        return state.reading

    async def activity(asset):
        state.activity_calls += 1
        return state.activity

    monkeypatch.setattr(bb, "_read", read)
    monkeypatch.setattr(bb, "_recent_activity", activity)
    monkeypatch.setattr(bb.time, "monotonic", clock)
    monkeypatch.setattr(bb, "_baseline", {})
    monkeypatch.setattr(bb, "_debits", {})
    state.clock = clock
    return state


async def test_good_reading_is_trusted(fake):
    fake.reading = Decimal("20.25")
    assert await bb.available_balance("USDC") == Decimal("20.25")


async def test_zero_after_debit_uses_baseline_minus_spend(fake):
    # The live sequence: 20.25, create a card ($4) and fund it ($3), then 0.
    fake.reading = Decimal("20.25")
    await bb.available_balance("USDC")
    fake.clock.now += 60
    bb.record_debit("USDC", Decimal("4.00"))
    bb.record_debit("USDC", Decimal("3.00"))
    fake.reading = Decimal("0")
    assert await bb.available_balance("USDC") == Decimal("13.25")
    assert fake.activity_calls == 0  # baseline answered it; no extra Bitnob call


async def test_estimate_never_negative(fake):
    fake.reading = Decimal("1.54")
    await bb.available_balance("USDC")
    bb.record_debit("USDC", Decimal("3.23"))
    fake.reading = Decimal("0")
    assert await bb.available_balance("USDC") == Decimal("0")


async def test_new_good_reading_resets_spend(fake):
    fake.reading = Decimal("20.25")
    await bb.available_balance("USDC")
    bb.record_debit("USDC", Decimal("7.00"))
    fake.clock.now += 300
    fake.reading = Decimal("14.25")  # the glitch cleared: this already includes the spend
    assert await bb.available_balance("USDC") == Decimal("14.25")
    fake.clock.now += 10
    fake.reading = Decimal("0")
    assert await bb.available_balance("USDC") == Decimal("14.25")  # not 14.25 - 7


async def test_stale_baseline_with_recent_activity_is_unknown(fake):
    fake.reading = Decimal("20.25")
    await bb.available_balance("USDC")
    fake.clock.now += bb.TRUST_WINDOW_SECONDS + 1
    fake.reading = Decimal("0")
    fake.activity = True
    assert await bb.available_balance("USDC") is None


async def test_no_baseline_recent_activity_is_unknown(fake):
    # e.g. the server just restarted in the middle of the glitch
    fake.activity = True
    assert await bb.available_balance("USDC") is None


async def test_no_baseline_no_activity_is_a_real_zero(fake):
    fake.activity = False
    assert await bb.available_balance("USDC") == Decimal("0")


async def test_missing_asset_is_unknown(fake):
    fake.reading = None
    assert await bb.available_balance("USDC") is None


async def test_recent_activity_fails_towards_true(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("Bitnob down")

    monkeypatch.setattr(bb, "request", boom)
    assert await bb._recent_activity("USDC") is True
