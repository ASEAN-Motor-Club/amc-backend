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


class GovSalaryMultiplierTestCase(TestCase):
    async def test_default_multiplier_is_2(self):
        policy = await sync_to_async(BankPolicy.load)()
        self.assertEqual(policy.gov_salary_multiplier, Decimal("2.000"))

    async def test_set_and_read_back(self):
        policy = await sync_to_async(BankPolicy.load)()
        policy.gov_salary_multiplier = Decimal("3.5")
        await policy.asave(update_fields=["gov_salary_multiplier"])
        mult = await sync_to_async(BankPolicy.get_gov_salary_multiplier)()
        self.assertEqual(mult, Decimal("3.5"))


class WealthTaxMultiplierTestCase(TestCase):
    async def test_default_multiplier_is_1(self):
        policy = await sync_to_async(BankPolicy.load)()
        self.assertEqual(policy.wealth_tax_multiplier, Decimal("1.000"))

    async def test_set_and_read_back(self):
        policy = await sync_to_async(BankPolicy.load)()
        policy.wealth_tax_multiplier = Decimal("0.5")
        await policy.asave(update_fields=["wealth_tax_multiplier"])
        mult = await sync_to_async(BankPolicy.get_wealth_tax_multiplier)()
        self.assertEqual(mult, Decimal("0.5"))

    async def test_apply_wealth_tax_multiplier_scales_tax(self):
        """Cron tax = bracket tax x BankPolicy.wealth_tax_multiplier."""
        from amc.factories import CharacterFactory
        from amc_finance.models import Account, JournalEntry
        from amc_finance.services import apply_wealth_tax, calculate_wealth_tax

        character = await sync_to_async(CharacterFactory)()
        character.last_online = timezone.now() - timedelta(days=60)  # pyrefly: ignore
        await character.asave(update_fields=["last_online"])

        balance = 5_000_000
        account = await Account.objects.acreate(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character=character,
            balance=balance,
        )

        policy = await sync_to_async(BankPolicy.load)()
        policy.wealth_tax_multiplier = Decimal("0.5")
        await policy.asave(update_fields=["wealth_tax_multiplier"])

        await apply_wealth_tax({})
        await account.arefresh_from_db()

        hours_offline = 60 * 24.0
        expected = int(calculate_wealth_tax(balance, hours_offline) * 0.5)
        self.assertEqual(account.balance, balance - expected)

        self.assertEqual(
            await JournalEntry.objects.filter(description="Wealth Tax").acount(), 1
        )

    async def test_apply_wealth_tax_zero_multiplier_disables(self):
        from amc.factories import CharacterFactory
        from amc_finance.models import Account, JournalEntry
        from amc_finance.services import apply_wealth_tax

        character = await sync_to_async(CharacterFactory)()
        character.last_online = timezone.now() - timedelta(days=60)  # pyrefly: ignore
        await character.asave(update_fields=["last_online"])

        await Account.objects.acreate(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character=character,
            balance=5_000_000,
        )

        policy = await sync_to_async(BankPolicy.load)()
        policy.wealth_tax_multiplier = Decimal("0.000")
        await policy.asave(update_fields=["wealth_tax_multiplier"])

        await apply_wealth_tax({})
        self.assertEqual(
            await JournalEntry.objects.filter(description="Wealth Tax").acount(), 0
        )

    async def test_preview_pages_shape(self):
        from amc_cogs.economy import (
            WEALTH_TAX_PREVIEW_BALANCES,
            _wt_interest_crossover_hours,
            _wealth_tax_preview_pages,
        )

        pages = _wealth_tax_preview_pages(
            Decimal("1.000"), Decimal("0.022"), Decimal("1.000")
        )
        self.assertEqual(len(pages), 2)
        self.assertIn("20M", pages[0])
        self.assertIn("crossover", pages[1])
        self.assertLessEqual(max(len(p) for p in pages), 1900)
        self.assertEqual(len(WEALTH_TAX_PREVIEW_BALANCES), 6)

        # Below the low bracket, interest never loses to wealth tax
        self.assertIsNone(_wt_interest_crossover_hours(5_000_000, 0.022))
