"""Create deterministic demo data for local development.

Usage:
    python -m scripts.seed_demo

The demo users all use the password ``DemoPass123!``. The script is
idempotent: records are assigned stable UUIDs and existing records are left
untouched when the script is run again.
"""

import asyncio
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel.ext.asyncio.session import AsyncSession  # noqa: E402

from src.auth.models import KycTier, ReferralRewardStatus, User  # noqa: E402
from src.auth.utils import hash_password  # noqa: E402
from src.cards.models import CardStatus, VirtualCard  # noqa: E402
from src.crossborder.models import CrossBorderStatus, CrossBorderTransfer  # noqa: E402
from src.db.main import engine  # noqa: E402
from src.splitbill.models import ShareStatus, SplitBill, SplitBillShare, SplitBillStatus  # noqa: E402
from src.vaults.models import (  # noqa: E402
    ContributionStatus,
    Vault,
    VaultContribution,
    VaultFrequency,
    VaultStatus,
)
from src.wallet.models import TransferStatus, WalletTransfer  # noqa: E402


DEMO_PASSWORD = "DemoPass123!"
NAMESPACE = uuid.UUID("4f5361c4-4ec2-4f64-bb2f-2c23ed0ac8a8")
NOW = datetime.now(timezone.utc)


def stable_id(name: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, name)


async def add_if_missing(session: AsyncSession, record: object, label: str) -> bool:
    record_id = getattr(record, "id")
    if await session.get(type(record), record_id) is not None:
        return False
    session.add(record)
    print(f"Added {label}")
    return True


async def seed() -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        users = {
            "ama": User(
                id=stable_id("user:ama"),
                phone_number="+233200000001",
                full_name="Ama Mensah",
                email="ama.demo@example.com",
                hashed_password=hash_password(DEMO_PASSWORD),
                is_phone_verified=True,
                kyc_tier=KycTier.PHONE_VERIFIED,  # verified phone -> matching limits tier
                referral_code="AMA2026",
                referral_reward_status=ReferralRewardStatus.REWARDED,
                default_momo_number="+233240000001",
                default_momo_bank_code="MTN",
                default_account_name="Ama Mensah",
            ),
            "kojo": User(
                id=stable_id("user:kojo"),
                phone_number="+233200000002",
                full_name="Kojo Owusu",
                email="kojo.demo@example.com",
                hashed_password=hash_password(DEMO_PASSWORD),
                is_phone_verified=True,
                kyc_tier=KycTier.PHONE_VERIFIED,  # verified phone -> matching limits tier
                referral_code="KOJO2026",
                referral_reward_status=ReferralRewardStatus.NONE,
                default_momo_number="+233240000002",
                default_momo_bank_code="VOD",
                default_account_name="Kojo Owusu",
            ),
            "efua": User(
                id=stable_id("user:efua"),
                phone_number="+233200000003",
                full_name="Efua Boateng",
                email="efua.demo@example.com",
                hashed_password=hash_password(DEMO_PASSWORD),
                is_phone_verified=True,
                kyc_tier=KycTier.PHONE_VERIFIED,  # verified phone -> matching limits tier
                referral_code="EFUA2026",
                referral_reward_status=ReferralRewardStatus.PENDING,
                default_momo_number="+233240000003",
                default_momo_bank_code="ATL",
                default_account_name="Efua Boateng",
            ),
        }
        for name, user in users.items():
            await add_if_missing(session, user, f"user {name}")
        await session.commit()

        ama = await session.get(User, users["ama"].id)
        kojo = await session.get(User, users["kojo"].id)
        efua = await session.get(User, users["efua"].id)
        if ama is None or kojo is None or efua is None:
            raise RuntimeError("Demo users were not available after insertion")

        vaults = {
            "holiday": Vault(
                id=stable_id("vault:ama:holiday"),
                owner_id=ama.id,
                name="December holiday",
                target_amount=Decimal("6000.00"),
                contribution_amount=Decimal("500.00"),
                frequency=VaultFrequency.MONTHLY,
                balance=Decimal("2750.00"),
                lock_until=date(2026, 12, 1),
            ),
            "emergency": Vault(
                id=stable_id("vault:kojo:emergency"),
                owner_id=kojo.id,
                name="Emergency fund",
                target_amount=Decimal("10000.00"),
                contribution_amount=Decimal("250.00"),
                frequency=VaultFrequency.MONTHLY,
                balance=Decimal("1250.00"),
                lock_until=date(2027, 1, 1),
            ),
            "school": Vault(
                id=stable_id("vault:efua:school"),
                owner_id=efua.id,
                name="School fees",
                target_amount=Decimal("4500.00"),
                contribution_amount=Decimal("300.00"),
                frequency=VaultFrequency.MONTHLY,
                balance=Decimal("900.00"),
                lock_until=date(2026, 10, 15),
            ),
        }
        for name, vault in vaults.items():
            await add_if_missing(session, vault, f"vault {name}")

        ama.round_up_vault_id = vaults["holiday"].id
        ama.round_up_denomination = Decimal("5.00")
        session.add(ama)
        await session.commit()

        contributions = [
            VaultContribution(
                id=stable_id("contribution:ama:1"),
                vault_id=vaults["holiday"].id,
                amount=Decimal("1500.00"),
                status=ContributionStatus.PAID,
                payment_reference="demo_vault_ama_001",
                paid_at=NOW - timedelta(days=45),
            ),
            VaultContribution(
                id=stable_id("contribution:ama:2"),
                vault_id=vaults["holiday"].id,
                amount=Decimal("1250.00"),
                status=ContributionStatus.PAID,
                payment_reference="demo_vault_ama_002",
                paid_at=NOW - timedelta(days=15),
            ),
            VaultContribution(
                id=stable_id("contribution:kojo:1"),
                vault_id=vaults["emergency"].id,
                amount=Decimal("1250.00"),
                status=ContributionStatus.PAID,
                payment_reference="demo_vault_kojo_001",
                paid_at=NOW - timedelta(days=10),
            ),
        ]
        for contribution in contributions:
            await add_if_missing(session, contribution, "vault contribution")

        transfers = [
            WalletTransfer(
                id=stable_id("wallet:ama:kojo:1"),
                sender_id=ama.id,
                recipient_id=kojo.id,
                gross_amount=Decimal("350.00"),
                platform_fee=Decimal("8.75"),
                net_amount=Decimal("341.25"),
                roundup_amount=Decimal("2.50"),
                note="Lunch and transport",
                status=TransferStatus.COMPLETED,
                payment_reference="demo_transfer_001",
                payout_reference="demo_payout_001",
                created_at=NOW - timedelta(days=3),
                completed_at=NOW - timedelta(days=3),
            ),
            WalletTransfer(
                id=stable_id("wallet:kojo:ama:1"),
                sender_id=kojo.id,
                recipient_id=ama.id,
                gross_amount=Decimal("800.00"),
                platform_fee=Decimal("20.00"),
                net_amount=Decimal("780.00"),
                note="Shared groceries",
                status=TransferStatus.COMPLETED,
                payment_reference="demo_transfer_002",
                payout_reference="demo_payout_002",
                created_at=NOW - timedelta(days=7),
                completed_at=NOW - timedelta(days=7),
            ),
            WalletTransfer(
                id=stable_id("wallet:efua:ama:pending"),
                sender_id=efua.id,
                recipient_id=ama.id,
                gross_amount=Decimal("120.00"),
                platform_fee=Decimal("3.00"),
                net_amount=Decimal("117.00"),
                note="Birthday contribution",
                status=TransferStatus.COMPLETED,
                payment_reference="demo_transfer_pending",
                created_at=NOW - timedelta(hours=4),
                completed_at=NOW - timedelta(hours=4),
            ),
        ]
        for transfer in transfers:
            await add_if_missing(session, transfer, "wallet transfer")

        card = VirtualCard(
            id=stable_id("card:ama:1"),
            user_id=ama.id,
            bitnob_card_id="demo-bitnob-card-001",
            bitnob_customer_id="demo-bitnob-customer-001",
            status=CardStatus.ACTIVE,
            masked_pan="424242******4242",
            card_brand="Visa",
            balance=Decimal("42.50"),
            initial_funding_ghs=Decimal("500.00"),
            payment_reference="demo_card_ama_001",
            dial_code="+233",
            local_phone_number="200000001",
        )
        await add_if_missing(session, card, "virtual card")

        await add_if_missing(
            session,
            CrossBorderTransfer(
                id=stable_id("crossborder:ama:1"),
                sender_id=ama.id,
                source_amount=Decimal("1200.00"),
                destination_country="NG",
                destination_currency="NGN",
                destination_amount=Decimal("102000.00"),
                exchange_rate_used=Decimal("85.00000000"),
                quote_id="demo-quote-001",
                bitnob_id="demo-payout-001",
                beneficiary_details={
                    "destination_type": "mobile_money",
                    "country": "NG",
                    "account_name": "Chinedu Okafor",
                    "account_number": "08030000001",
                    "network": "MTN",
                },
                status=CrossBorderStatus.COMPLETED,
                payment_reference="demo_crossborder_001",
                bitnob_status="completed",
                created_at=NOW - timedelta(days=2),
                completed_at=NOW - timedelta(days=2),
            ),
            "cross-border transfer",
        )

        bill = SplitBill(
            id=stable_id("split-bill:team-dinner"),
            organizer_id=ama.id,
            title="Team dinner",
            total_amount=Decimal("900.00"),
            status=SplitBillStatus.OPEN,
            created_at=NOW - timedelta(days=1),
        )
        await add_if_missing(session, bill, "split bill")
        for name, user_id, amount, status in [
            ("ama", ama.id, Decimal("300.00"), ShareStatus.PAID),
            ("kojo", kojo.id, Decimal("300.00"), ShareStatus.PAID),
            ("efua", efua.id, Decimal("300.00"), ShareStatus.PAID),
        ]:
            await add_if_missing(
                session,
                SplitBillShare(
                    id=stable_id(f"split-share:team-dinner:{name}"),
                    split_bill_id=bill.id,
                    user_id=user_id,
                    gross_amount=amount,
                    platform_fee=Decimal("7.50"),
                    net_amount=Decimal("292.50"),
                    status=status,
                    payment_reference=f"demo_split_{name}",
                    paid_at=NOW - timedelta(days=1) if status == ShareStatus.PAID else None,
                ),
                "split bill share",
            )

        await session.commit()
        print("Demo seed complete.")
        print("Demo login password: DemoPass123!")
        print("Demo phone numbers: +233200000001, +233200000002, +233200000003")


if __name__ == "__main__":
    asyncio.run(seed())