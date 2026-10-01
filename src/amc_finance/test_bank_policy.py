from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from asgiref.sync import sync_to_async
from amc.factories import CharacterFactory
from amc_finance.models import Account, BankPolicy, JournalEntry
from amc_finance.services import (
    apply_interest_to_bank_accounts,
    calculate_hourly_interest,
)


class BankPolicyTestCase(TestCase):
    async def test_load_creates_singleton_with_default_rate(self):
        policy = await sync_to_async(BankPolicy.load)()
        self.assertEqual(policy.pk, 1)
        self.assertEqual(policy.daily_interest_rate, Decimal("0.022"))

        again = await sync_to_async(BankPolicy.load)()
        self.assertEqual(again.pk, 1)
        count = await BankPolicy.objects.acount()
        self.assertEqual(count, 1)

    async def test_get_daily_interest_rate_reflects_saved_value(self):
        policy = await sync_to_async(BankPolicy.load)()
        policy.daily_interest_rate = Decimal("0.05")
        await policy.asave(update_fields=["daily_interest_rate"])
        rate = await sync_to_async(BankPolicy.get_daily_interest_rate)()
        self.assertEqual(rate, Decimal("0.05"))


class InterestRateSourceTestCase(TestCase):
    async def _make_account(self, balance):
        character = await sync_to_async(CharacterFactory)()
        return await Account.objects.acreate(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character=character,
            balance=balance,
        )

    async def test_calculate_hourly_interest_uses_bankpolicy_rate(self):
        policy = await sync_to_async(BankPolicy.load)()
        policy.daily_interest_rate = Decimal("0.048")
        await policy.asave(update_fields=["daily_interest_rate"])

        # 2x online multiplier (last_online now) → rate 0.048 * 2 = 0.096
        character = await sync_to_async(CharacterFactory)()
        character.last_online = timezone.now() - timedelta(minutes=5)  # pyrefly: ignore
        await character.asave(update_fields=["last_online"])

        interest = await sync_to_async(calculate_hourly_interest)(1_000_000, 0.083)
        self.assertEqual(interest, int(1_000_000 * Decimal("0.096") / Decimal(24)))

    async def test_calculate_hourly_interest_explicit_rate_overrides(self):
        interest = await sync_to_async(calculate_hourly_interest)(
            1_000_000, 0.5, interest_rate=0.022
        )
        self.assertEqual(interest, int(1_000_000 * Decimal("0.044") / Decimal(24)))

    async def test_apply_interest_zero_rate_posts_nothing(self):
        policy = await sync_to_async(BankPolicy.load)()
        policy.daily_interest_rate = Decimal(0)
        await policy.asave(update_fields=["daily_interest_rate"])

        account = await self._make_account(1_000_000)
        await apply_interest_to_bank_accounts({})
        await account.arefresh_from_db()
        self.assertEqual(account.balance, 1_000_000)

        journals = JournalEntry.objects.filter(description="Interest Payment")
        self.assertEqual(await journals.acount(), 0)

    async def test_apply_interest_uses_bankpolicy_rate(self):
        policy = await sync_to_async(BankPolicy.load)()
        policy.daily_interest_rate = Decimal("0.048")
        await policy.asave(update_fields=["daily_interest_rate"])

        account = await self._make_account(1_000_000)
        await apply_interest_to_bank_accounts({})
        await account.arefresh_from_db()
        # Character has no last_online → 365d decay → less than flat 4.8%/24h
        flat = 1_000_000 * Decimal("0.048") / Decimal(24)
        self.assertGreater(account.balance, 1_000_000)
        self.assertLess(account.balance, 1_000_000 + flat)

    async def test_apply_interest_explicit_rate_still_overrides(self):
        account = await self._make_account(1_000_000)
        await apply_interest_to_bank_accounts({}, interest_rate=0.048)
        await account.arefresh_from_db()
        flat = 1_000_000 * Decimal("0.048") / Decimal(24)
        self.assertGreater(account.balance, 1_000_000)
        self.assertLess(account.balance, 1_000_000 + flat)

    async def test_apply_negative_interest_charges_account(self):
        account = await self._make_account(1_000_000)
        await apply_interest_to_bank_accounts({}, interest_rate=-0.024)
        await account.arefresh_from_db()
        # No last_online → 365d log decay shrinks the magnitude below flat
        flat_charge = 1_000_000 * Decimal("0.024") / Decimal(24)
        self.assertLess(account.balance, 1_000_000)
        self.assertGreater(account.balance, 1_000_000 - flat_charge)
        journals = JournalEntry.objects.filter(
            description="Interest Charge", entries__account=account
        ).distinct()
        self.assertEqual(await journals.acount(), 1)
        # Journal balanced: debit == credit
        je = await journals.afirst()
        legs = [le async for le in je.entries.all()]
        self.assertEqual(
            sum(le.debit for le in legs), sum(le.credit for le in legs)
        )
        # Player's checking leg is the debit
        player_leg = next(le for le in legs if le.account_id == account.id)
        self.assertEqual(player_leg.debit, sum(le.debit for le in legs))
        self.assertEqual(player_leg.credit, 0)
