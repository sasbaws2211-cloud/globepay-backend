import csv
import io
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.models import ReferralRewardStatus, User
from src.common.kyc_limits import check_transaction_limit, record_transaction_volume
from src.common.locking import locked_first
from src.common.sms import send_sms
from src.config import settings
from src.payments import paystack
from src.vaults.models import (
    ContributionStatus,
    RecurringStatus,
    Vault,
    VaultContribution,
    VaultFrequency,
    VaultStatus,
    VaultWithdrawal,
    WithdrawalStatus,
)
from src.vaults.schemas import VaultCreate, WithdrawalRequest

logger = logging.getLogger(__name__)

VAULT_PAYOUT_REFERENCE_PREFIX = "vault-wd-"
REFERRAL_BONUS_GHS = Decimal("5.00")
MAX_RECURRING_FAILURES = 3

# DAILY has no defined offset - allowing a "recurring daily card charge"
# invites far more failed-charge noise than a savings feature needs, so
# recurring is only offered for weekly/monthly vaults (enforced in
# enable_recurring below).
_RECURRING_OFFSET = {VaultFrequency.WEEKLY: timedelta(days=7), VaultFrequency.MONTHLY: timedelta(days=30)}


async def create_vault(session: AsyncSession, owner_id: uuid.UUID, data: VaultCreate) -> Vault:
    vault = Vault(owner_id=owner_id, **data.model_dump())
    session.add(vault)
    await session.commit()
    await session.refresh(vault)
    return vault


async def list_user_vaults(session: AsyncSession, owner_id: uuid.UUID) -> list[Vault]:
    result = await session.exec(select(Vault).where(Vault.owner_id == owner_id))
    return list(result.all())


async def get_owned_vault(session: AsyncSession, vault_id: uuid.UUID, owner_id: uuid.UUID) -> Vault:
    vault = await session.get(Vault, vault_id)
    if vault is None or vault.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Vault not found")
    return vault


async def cancel_vault(session: AsyncSession, vault: Vault) -> Vault:
    """For a vault created by mistake (wrong name/target/frequency) with
    nothing paid into it yet - not a way to abandon a vault that's already
    holding real money, which must go through request_withdrawal instead."""
    if vault.status != VaultStatus.ACTIVE:
        raise HTTPException(status_code=400, detail=f"Cannot cancel a '{vault.status}' vault")
    if vault.balance > 0:
        raise HTTPException(status_code=400, detail="Cannot cancel a vault with a balance - withdraw first")

    vault.status = VaultStatus.CANCELLED
    # A cancelled vault must not keep a live recurring schedule - the
    # scheduler only checks recurring_status, not vault.status, when
    # deciding what to charge next.
    vault.recurring_status = RecurringStatus.INACTIVE
    vault.next_charge_date = None
    vault.updated_at = datetime.now(timezone.utc)
    session.add(vault)
    await session.commit()
    await session.refresh(vault)
    return vault


async def _credit_referral_bonus(session: AsyncSession, user: User) -> bool:
    """Credits REFERRAL_BONUS_GHS into `user`'s oldest vault as an
    already-paid contribution - same shape as credit_roundup, no separate
    Paystack call, since this is money the platform itself is giving out
    (funded from fee revenue), not money collected from this user. Returns
    False (and credits nothing) if the user has no vault to receive it -
    a known limitation of this demo design rather than something solved
    with a fallback destination."""
    result = await session.exec(select(Vault).where(Vault.owner_id == user.id).order_by(Vault.created_at.asc()))
    vault = result.first()
    if vault is None:
        return False

    reference = f"referral-bonus-{uuid.uuid4().hex[:10]}"
    session.add(
        VaultContribution(
            vault_id=vault.id,
            amount=REFERRAL_BONUS_GHS,
            status=ContributionStatus.PAID,
            payment_reference=reference,
            paid_at=datetime.now(timezone.utc),
        )
    )
    vault.balance += REFERRAL_BONUS_GHS
    if vault.balance >= vault.target_amount:
        vault.status = VaultStatus.MATURED
    vault.updated_at = datetime.now(timezone.utc)
    session.add(vault)
    return True


async def _maybe_reward_referral(session: AsyncSession, referred_user_id: uuid.UUID) -> None:
    """Fires once, the moment a referred user's very first vault
    contribution lands as PAID (via direct payment or a round-up credit -
    both call this). Deliberately gated on a real deposit, not just
    registering with a referral code, so the bonus can't be farmed by
    signing up alone."""
    referred_user = await session.get(User, referred_user_id)
    if referred_user is None or referred_user.referral_reward_status != ReferralRewardStatus.PENDING:
        return

    paid_count_result = await session.exec(
        select(func.count())
        .select_from(VaultContribution)
        .join(Vault, VaultContribution.vault_id == Vault.id)
        .where(Vault.owner_id == referred_user.id, VaultContribution.status == ContributionStatus.PAID)
    )
    if paid_count_result.one() != 1:
        return  # not their first ever paid contribution

    referrer = await session.get(User, referred_user.referred_by_id)
    if referrer is None:
        return

    credited_referred = await _credit_referral_bonus(session, referred_user)
    credited_referrer = await _credit_referral_bonus(session, referrer)

    referred_user.referral_reward_status = ReferralRewardStatus.REWARDED
    session.add(referred_user)

    if credited_referred:
        await send_sms(
            referred_user.phone_number,
            f"You just earned a GHS {REFERRAL_BONUS_GHS} welcome bonus in your vault for joining via a friend's invite!",
        )
    if credited_referrer:
        await send_sms(
            referrer.phone_number,
            f"{referred_user.full_name} made their first deposit - you earned a GHS {REFERRAL_BONUS_GHS} "
            f"referral bonus in your vault!",
        )


async def credit_roundup(session: AsyncSession, vault_id: uuid.UUID, amount: Decimal, reference: str) -> None:
    """Sweep a round-up amount into a vault as an already-paid contribution.
    Called after the parent wallet transfer that carried this round-up has
    itself been confirmed paid - no separate Paystack call needed, the
    money was already collected in that charge."""
    if amount <= 0:
        return

    contribution = VaultContribution(
        vault_id=vault_id,
        amount=amount,
        status=ContributionStatus.PAID,
        payment_reference=reference,
        paid_at=datetime.now(timezone.utc),
    )
    session.add(contribution)

    vault = await session.get(Vault, vault_id)
    vault.balance += amount
    if vault.balance >= vault.target_amount:
        vault.status = VaultStatus.MATURED
    vault.updated_at = datetime.now(timezone.utc)
    session.add(vault)

    await _maybe_reward_referral(session, vault.owner_id)


async def initiate_contribution(
    session: AsyncSession, vault: Vault, amount: Decimal, email: str
) -> dict:
    # WITHDRAWN/CANCELLED are terminal - the vault relationship is over, and
    # accepting money into one would create an orphaned balance nothing
    # ever pays back out (a WITHDRAWN vault already emptied to the owner's
    # momo once; there's no second withdrawal path waiting for it).
    # MATURED is deliberately still allowed - reaching the target doesn't
    # mean the owner wants to stop saving into it.
    if vault.status in (VaultStatus.WITHDRAWN, VaultStatus.CANCELLED):
        raise HTTPException(status_code=400, detail=f"Cannot contribute to a '{vault.status}' vault")

    owner = await session.get(User, vault.owner_id)
    await check_transaction_limit(session, owner, amount)

    reference = f"vault-{vault.id}-{uuid.uuid4().hex[:10]}"
    contribution = VaultContribution(
        vault_id=vault.id, amount=amount, payment_reference=reference
    )
    session.add(contribution)
    await session.commit()

    data = await paystack.initialize_transaction(
        email=email,
        amount=amount,
        reference=reference,
        metadata={"vault_id": str(vault.id), "type": "vault_contribution"},
    )
    return {"authorization_url": data["authorization_url"], "reference": reference}


async def _credit_confirmed_contribution(session: AsyncSession, contribution: VaultContribution) -> Vault:
    """Shared by both ways a contribution gets marked PAID from a real
    charge: the webhook-confirmed one-off payment below, and the scheduled
    recurring charge in run_recurring_charge. credit_roundup and the
    referral bonus have their own near-identical inline version since
    they're not backed by a real Paystack charge (see their own docstrings
    for why)."""
    contribution.status = ContributionStatus.PAID
    contribution.paid_at = datetime.now(timezone.utc)
    session.add(contribution)

    vault = await session.get(Vault, contribution.vault_id)
    vault.balance += contribution.amount
    if vault.balance >= vault.target_amount:
        vault.status = VaultStatus.MATURED
    vault.updated_at = datetime.now(timezone.utc)
    session.add(vault)

    record_transaction_volume(session, vault.owner_id, contribution.amount, "vault_contribution")
    await _maybe_reward_referral(session, vault.owner_id)
    return vault


def _save_card_authorization(user: User, verified: dict) -> None:
    """Captures a reusable card authorization from a successful charge's
    verify response so it can later be used for recurring contributions
    (paystack.charge_authorization). Only card authorizations are saved -
    see the field's docstring on User for why mobile money isn't."""
    authorization = verified.get("authorization") or {}
    if authorization.get("reusable") and authorization.get("channel") == "card" and authorization.get("authorization_code"):
        user.paystack_authorization_code = authorization["authorization_code"]
        user.paystack_card_last4 = authorization.get("last4")


async def confirm_contribution(session: AsyncSession, reference: str) -> VaultContribution:
    """Called from the Paystack webhook handler once a charge succeeds, and
    from the refresh/reconcile pollers - locked so they can't double-credit."""
    contribution = await locked_first(session, VaultContribution, VaultContribution.payment_reference == reference)
    if contribution is None:
        raise HTTPException(status_code=404, detail="Contribution not found")

    if contribution.status == ContributionStatus.PAID:
        return contribution  # already processed, avoid double-crediting

    verified = await paystack.verify_transaction(reference)
    if verified.get("status") != "success":
        contribution.status = ContributionStatus.FAILED
        session.add(contribution)
        await session.commit()
        return contribution

    vault = await _credit_confirmed_contribution(session, contribution)

    owner = await session.get(User, vault.owner_id)
    _save_card_authorization(owner, verified)
    session.add(owner)

    await session.commit()
    await session.refresh(contribution)
    return contribution


async def request_withdrawal(
    session: AsyncSession, vault: Vault, payload: WithdrawalRequest
) -> VaultWithdrawal:
    # Lock the vault row so a double-tapped Withdraw can't pass the checks
    # below twice and fire two payouts of the same balance. populate_existing
    # re-reads the row even though `vault` is already in the identity map.
    result = await session.exec(
        select(Vault).where(Vault.id == vault.id).with_for_update().execution_options(populate_existing=True)
    )
    vault = result.one()

    if vault.status == VaultStatus.WITHDRAWN:
        raise HTTPException(status_code=400, detail="Vault already withdrawn")

    if date.today() < vault.lock_until:
        raise HTTPException(
            status_code=400,
            detail=f"Vault is locked until {vault.lock_until.isoformat()}",
        )

    if vault.balance <= 0:
        raise HTTPException(status_code=400, detail="Nothing to withdraw")

    gross = vault.balance
    fee = (gross * Decimal(settings.PLATFORM_WITHDRAWAL_FEE_PERCENT) / Decimal(100)).quantize(
        Decimal("0.01")
    )
    net = gross - fee

    withdrawal = VaultWithdrawal(
        vault_id=vault.id, gross_amount=gross, platform_fee=fee, net_amount=net
    )
    session.add(withdrawal)

    try:
        recipient_code = await paystack.create_transfer_recipient(
            name=payload.account_name,
            account_number=payload.momo_number,
            bank_code=payload.momo_network_bank_code,
        )
        reference = f"{VAULT_PAYOUT_REFERENCE_PREFIX}{withdrawal.id}"
        transfer = await paystack.initiate_transfer(
            amount=net, recipient_code=recipient_code, reason="Vault withdrawal", reference=reference
        )
    except paystack.PaystackError as exc:
        # Nothing was sent - drop the uncommitted withdrawal and leave the
        # vault untouched so the owner can fix their details and retry.
        await session.rollback()
        raise HTTPException(status_code=502, detail=f"Withdrawal could not be started: {exc}") from exc

    # PENDING until the transfer.success / transfer.failed webhook lands -
    # see handle_payout_event. Paystack accepting the request isn't delivery.
    withdrawal.payout_reference = transfer.get("reference", reference)
    withdrawal.status = WithdrawalStatus.PENDING
    if transfer.get("status") == "otp":
        logger.warning(
            "Vault withdrawal payout %s is held for OTP - Transfer OTP is enabled on the Paystack "
            "account, so it won't be sent until finalized in the Paystack dashboard.",
            withdrawal.payout_reference,
        )

    vault.balance = Decimal("0.00")
    vault.status = VaultStatus.WITHDRAWN
    vault.updated_at = datetime.now(timezone.utc)

    session.add(withdrawal)
    session.add(vault)
    await session.commit()
    await session.refresh(withdrawal)

    owner = await session.get(User, vault.owner_id)
    await send_sms(
        owner.phone_number,
        f"Your '{vault.name}' vault withdrawal of GHS {withdrawal.net_amount} "
        f"(after GHS {withdrawal.platform_fee} fee) is on its way to your mobile money wallet.",
    )

    return withdrawal


async def handle_payout_event(session: AsyncSession, event: str, reference: str) -> VaultWithdrawal | None:
    """Paystack transfer.success / transfer.failed / transfer.reversed webhook
    for a vault withdrawal payout."""
    result = await session.exec(
        select(VaultWithdrawal)
        .where(VaultWithdrawal.payout_reference == reference)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    withdrawal = result.first()
    if withdrawal is None or withdrawal.status != WithdrawalStatus.PENDING:
        return withdrawal  # unknown/stale reference or already processed (duplicate delivery)

    vault_result = await session.exec(
        select(Vault).where(Vault.id == withdrawal.vault_id).with_for_update().execution_options(populate_existing=True)
    )
    vault = vault_result.one()
    owner = await session.get(User, vault.owner_id)

    if event == "transfer.success":
        withdrawal.status = WithdrawalStatus.COMPLETED
        await send_sms(
            owner.phone_number,
            f"Your '{vault.name}' vault withdrawal of GHS {withdrawal.net_amount} has been delivered "
            f"to your mobile money wallet.",
        )
    else:
        # Failed or reversed: the money is back in the platform's Paystack
        # balance, so it must go back into the vault rather than vanish.
        # The fee was never realized either (admin stats exclude FAILED).
        logger.warning("Vault withdrawal payout %s: %s - restoring vault balance", reference, event)
        withdrawal.status = WithdrawalStatus.FAILED
        vault.balance += withdrawal.gross_amount
        vault.status = VaultStatus.ACTIVE
        vault.updated_at = datetime.now(timezone.utc)
        session.add(vault)
        await send_sms(
            owner.phone_number,
            f"We couldn't deliver your '{vault.name}' withdrawal to your mobile money wallet. "
            f"GHS {withdrawal.gross_amount} is back in your vault - check your details and withdraw again.",
        )

    session.add(withdrawal)
    await session.commit()
    await session.refresh(withdrawal)
    return withdrawal


async def enable_recurring(session: AsyncSession, vault: Vault, owner: User) -> Vault:
    if vault.frequency not in _RECURRING_OFFSET:
        raise HTTPException(
            status_code=400, detail="Recurring savings is only available for weekly or monthly vaults"
        )
    if vault.status != VaultStatus.ACTIVE:
        raise HTTPException(status_code=400, detail=f"Cannot enable recurring on a '{vault.status}' vault")
    if not owner.paystack_authorization_code:
        raise HTTPException(
            status_code=400,
            detail="Make at least one card payment on this platform first, so we have a card saved to charge automatically",
        )

    vault.recurring_status = RecurringStatus.ACTIVE
    vault.next_charge_date = (datetime.now(timezone.utc) + _RECURRING_OFFSET[vault.frequency]).date()
    vault.recurring_consecutive_failures = 0
    vault.recurring_last_failure_reason = None
    vault.updated_at = datetime.now(timezone.utc)
    session.add(vault)
    await session.commit()
    await session.refresh(vault)
    return vault


async def pause_recurring(session: AsyncSession, vault: Vault) -> Vault:
    if vault.recurring_status not in (RecurringStatus.ACTIVE, RecurringStatus.SUSPENDED):
        raise HTTPException(status_code=400, detail="Recurring savings is not active on this vault")
    vault.recurring_status = RecurringStatus.PAUSED
    vault.updated_at = datetime.now(timezone.utc)
    session.add(vault)
    await session.commit()
    await session.refresh(vault)
    return vault


async def resume_recurring(session: AsyncSession, vault: Vault) -> Vault:
    if vault.recurring_status != RecurringStatus.PAUSED:
        raise HTTPException(status_code=400, detail="Recurring savings is not paused on this vault")
    vault.recurring_status = RecurringStatus.ACTIVE
    vault.next_charge_date = (datetime.now(timezone.utc) + _RECURRING_OFFSET[vault.frequency]).date()
    vault.recurring_consecutive_failures = 0
    vault.recurring_last_failure_reason = None
    vault.updated_at = datetime.now(timezone.utc)
    session.add(vault)
    await session.commit()
    await session.refresh(vault)
    return vault


async def cancel_recurring(session: AsyncSession, vault: Vault) -> Vault:
    vault.recurring_status = RecurringStatus.INACTIVE
    vault.next_charge_date = None
    vault.recurring_consecutive_failures = 0
    vault.recurring_last_failure_reason = None
    vault.updated_at = datetime.now(timezone.utc)
    session.add(vault)
    await session.commit()
    await session.refresh(vault)
    return vault


async def run_recurring_charge(session: AsyncSession, vault: Vault) -> None:
    """Called from the scheduled job in src/scheduler.py for one due vault
    at a time. Always advances next_charge_date regardless of outcome (a
    failed run waits for the *next* scheduled date rather than retrying on
    every subsequent sweep), and auto-suspends after MAX_RECURRING_FAILURES
    in a row so a permanently-declined card doesn't fail silently forever."""
    owner = await session.get(User, vault.owner_id)
    vault.next_charge_date = (datetime.now(timezone.utc) + _RECURRING_OFFSET[vault.frequency]).date()

    async def _fail(reason: str) -> None:
        vault.recurring_consecutive_failures += 1
        vault.recurring_last_failure_reason = reason
        if vault.recurring_consecutive_failures >= MAX_RECURRING_FAILURES:
            vault.recurring_status = RecurringStatus.SUSPENDED
            await send_sms(
                owner.phone_number,
                f"Your recurring savings into '{vault.name}' has been paused after {MAX_RECURRING_FAILURES} "
                f"failed attempts ({reason}). Resume it once the issue is fixed.",
            )

    if not owner.paystack_authorization_code:
        await _fail("no saved card")
        session.add(vault)
        await session.commit()
        return

    try:
        await check_transaction_limit(session, owner, vault.contribution_amount)
    except HTTPException as e:
        await _fail(str(e.detail))
        session.add(vault)
        await session.commit()
        return

    reference = f"vault-recurring-{vault.id}-{uuid.uuid4().hex[:10]}"
    contribution = VaultContribution(vault_id=vault.id, amount=vault.contribution_amount, payment_reference=reference)
    session.add(contribution)

    try:
        charge_result = await paystack.charge_authorization(
            authorization_code=owner.paystack_authorization_code,
            email=owner.email or f"{owner.phone_number}@example.com",
            amount=vault.contribution_amount,
            reference=reference,
        )
    except paystack.PaystackError as e:
        contribution.status = ContributionStatus.FAILED
        session.add(contribution)
        await _fail(str(e))
        session.add(vault)
        await session.commit()
        return

    if charge_result.get("status") != "success":
        contribution.status = ContributionStatus.FAILED
        session.add(contribution)
        await _fail(charge_result.get("gateway_response", "charge declined"))
        session.add(vault)
        await session.commit()
        return

    await _credit_confirmed_contribution(session, contribution)
    vault.recurring_consecutive_failures = 0
    vault.recurring_last_failure_reason = None
    session.add(vault)
    await session.commit()

    await send_sms(
        owner.phone_number,
        f"Your scheduled GHS {vault.contribution_amount} contribution to '{vault.name}' went through.",
    )


async def sweep_recurring_charges(session: AsyncSession) -> int:
    """Called from the scheduled job in src/scheduler.py - finds every
    ACTIVE recurring vault whose next charge date has arrived. Returns the
    number processed (for the job's own logging)."""
    result = await session.exec(
        select(Vault).where(Vault.recurring_status == RecurringStatus.ACTIVE, Vault.next_charge_date <= date.today())
    )
    vaults = list(result.all())
    for vault in vaults:
        await run_recurring_charge(session, vault)
    return len(vaults)


async def generate_vault_statement_csv(session: AsyncSession, vault: Vault) -> str:
    """A plain-CSV statement (no new dependency for PDF generation) of every
    contribution and withdrawal on this vault, in chronological order, with
    a running balance - meant to double as informal proof of savings."""
    contributions_result = await session.exec(
        select(VaultContribution)
        .where(VaultContribution.vault_id == vault.id, VaultContribution.status == ContributionStatus.PAID)
        .order_by(VaultContribution.paid_at.asc())
    )
    # PENDING means a real Paystack payout was initiated and is in flight
    # (handle_payout_event moves it to COMPLETED or FAILED), so it belongs on
    # the statement. FAILED is excluded - its amount was restored to the vault.
    withdrawals_result = await session.exec(
        select(VaultWithdrawal)
        .where(VaultWithdrawal.vault_id == vault.id, VaultWithdrawal.status != WithdrawalStatus.FAILED)
        .order_by(VaultWithdrawal.created_at.asc())
    )

    entries = [
        (c.paid_at, "Contribution", c.amount, c.payment_reference or "") for c in contributions_result.all()
    ] + [
        (w.created_at, "Withdrawal", -w.net_amount, w.payout_reference or "") for w in withdrawals_result.all()
    ]
    entries.sort(key=lambda row: row[0])

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([f"Statement for vault: {vault.name}"])
    writer.writerow([f"Target: GHS {vault.target_amount}", f"Current balance: GHS {vault.balance}", f"Status: {vault.status}"])
    writer.writerow([f"Generated: {datetime.now(timezone.utc).isoformat()}"])
    writer.writerow([])
    writer.writerow(["Date", "Type", "Amount (GHS)", "Reference", "Running Balance (GHS)"])

    running = Decimal("0.00")
    for when, kind, amount, reference in entries:
        running += amount
        writer.writerow([when.isoformat(), kind, f"{amount:.2f}", reference, f"{running:.2f}"])

    return buffer.getvalue()
