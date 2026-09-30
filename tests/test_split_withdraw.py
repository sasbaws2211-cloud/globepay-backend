"""Split bills - collect then withdraw: what's withdrawable, and the request shape."""

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.splitbill.models import ShareStatus, SplitBill, SplitBillShare, WithdrawalDestination
from src.splitbill.schemas import SplitWithdrawRequest
from src.splitbill.service import collected_amount, collected_gross, withdrawal_amounts

ORGANIZER = uuid.uuid4()


def bill():
    return SplitBill(organizer_id=ORGANIZER, title="Dinner", total_amount=Decimal("90"))


def share(user_id, net, status, gross=None):
    gross = Decimal(gross or net)
    return SplitBillShare(split_bill_id=uuid.uuid4(), user_id=user_id, gross_amount=gross,
                          platform_fee=gross - Decimal(net), net_amount=Decimal(net), status=status)


def test_vault_withdrawal_waives_the_fee_momo_pays_it():
    b = bill()
    shares = [
        share(ORGANIZER, "30.00", ShareStatus.PAID),
        share(uuid.uuid4(), "29.25", ShareStatus.PAID, gross="30.00"),   # 2.5% fee = 0.75
        share(uuid.uuid4(), "29.25", ShareStatus.PAID, gross="30.00"),
        share(uuid.uuid4(), "29.25", ShareStatus.CANCELLED, gross="30.00"),
    ]
    assert collected_gross(b, shares) == Decimal("60.00")
    assert withdrawal_amounts(b, shares, WithdrawalDestination.VAULT) == (Decimal("60.00"), Decimal("0.00"))
    assert withdrawal_amounts(b, shares, WithdrawalDestination.MOMO) == (Decimal("58.50"), Decimal("1.50"))


def test_collected_is_net_of_paid_participant_shares_only():
    b = bill()
    shares = [
        share(ORGANIZER, "30.00", ShareStatus.PAID),         # organizer paid upfront - never collected
        share(uuid.uuid4(), "29.25", ShareStatus.PAID),
        share(uuid.uuid4(), "29.25", ShareStatus.PENDING),   # not paid yet
        share(uuid.uuid4(), "29.25", ShareStatus.CANCELLED), # dropped when the bill was closed
    ]
    assert collected_amount(b, shares) == Decimal("29.25")


def test_nothing_collected():
    b = bill()
    assert collected_amount(b, [share(ORGANIZER, "30", ShareStatus.PAID)]) == Decimal("0.00")


def test_new_bills_collect_by_default():
    assert bill().collects_funds is True


def test_vault_destination_needs_a_vault():
    with pytest.raises(ValidationError):
        SplitWithdrawRequest(destination="vault")
    assert SplitWithdrawRequest(destination="vault", vault_id=uuid.uuid4()).vault_id is not None


def test_momo_destination_needs_no_vault():
    assert SplitWithdrawRequest(destination="momo").vault_id is None


def test_unknown_destination_refused():
    with pytest.raises(ValidationError):
        SplitWithdrawRequest(destination="bank")
