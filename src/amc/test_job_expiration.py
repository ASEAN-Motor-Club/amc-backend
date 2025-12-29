from datetime import timedelta
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from asgiref.sync import sync_to_async
from amc.models import MinistryTerm
from amc.factories import DeliveryJobFactory, PlayerFactory
from amc_finance.models import Account
from amc_finance.services import (
    process_ministry_expiration,
    process_treasury_expiration_penalty,
    get_treasury_fund_balance,
    allocate_ministry_budget,
    escrow_ministry_funds,
)


class TreasuryExpirationPenaltyTestCase(TestCase):
    """Tests for treasury penalty during government shutdown (no MinistryTerm)."""

    async def test_treasury_penalty_for_expired_job(self):
        """50% of completion_bonus should be charged to treasury."""
        # Setup: Get initial treasury balance
        initial_balance = await get_treasury_fund_balance()

        # Create a non-ministry funded job (government shutdown scenario)
        job = await sync_to_async(DeliveryJobFactory)(
            completion_bonus=100_000,
            expired_at=timezone.now() - timedelta(hours=1),
            funding_term=None,
            escrowed_amount=0,
        )

        # Execute
        await process_treasury_expiration_penalty(job)

        # Assert: Treasury should be reduced by 50% of completion bonus
        final_balance = await get_treasury_fund_balance()
        expected_penalty = 50_000  # 50% of 100,000
        self.assertEqual(initial_balance - expected_penalty, final_balance)

    async def test_completion_bonus_zeroed_after_penalty(self):
        """completion_bonus should be zeroed to prevent double processing."""
        job = await sync_to_async(DeliveryJobFactory)(
            completion_bonus=100_000,
            expired_at=timezone.now() - timedelta(hours=1),
            funding_term=None,
        )

        await process_treasury_expiration_penalty(job)

        await job.arefresh_from_db()
        self.assertEqual(job.completion_bonus, 0)

    async def test_no_penalty_for_zero_bonus_job(self):
        """Jobs with 0 completion_bonus should not affect treasury."""
        initial_balance = await get_treasury_fund_balance()

        job = await sync_to_async(DeliveryJobFactory)(
            completion_bonus=0,
            expired_at=timezone.now() - timedelta(hours=1),
            funding_term=None,
        )

        await process_treasury_expiration_penalty(job)

        final_balance = await get_treasury_fund_balance()
        self.assertEqual(initial_balance, final_balance)


class MinistryExpirationTestCase(TestCase):
    """Tests for ministry-funded job expiration (existing behavior)."""

    async def test_ministry_expiration_refunds_and_burns(self):
        """Ministry funded jobs should refund 50% and burn 50%."""
        # Setup: Create ministry term
        player = await sync_to_async(PlayerFactory)()
        term = await MinistryTerm.objects.acreate(
            minister=player,
            start_date=timezone.now() - timedelta(days=1),
            end_date=timezone.now() + timedelta(days=30),
            initial_budget=Decimal("1000000"),
            current_budget=Decimal("1000000"),
        )

        # Allocate ministry budget
        await allocate_ministry_budget(1_000_000, term)

        # Create a ministry-funded job
        job = await sync_to_async(DeliveryJobFactory)(
            completion_bonus=100_000,
            expired_at=timezone.now() - timedelta(hours=1),
            funding_term=term,
            escrowed_amount=0,
        )

        # Escrow funds
        await escrow_ministry_funds(100_000, job)
        job.escrowed_amount = 100_000
        await job.asave()

        # Get ministry budget account balance before expiration
        ministry_budget = await Account.objects.aget(
            book=Account.Book.GOVERNMENT,
            name="Ministry of Commerce Budget",
        )
        initial_budget_balance = ministry_budget.balance

        # Execute
        await process_ministry_expiration(job)

        # Assert: 50% refunded to budget
        await ministry_budget.arefresh_from_db()
        expected_refund = 50_000  # 50% of 100,000
        self.assertEqual(
            initial_budget_balance + expected_refund, ministry_budget.balance
        )

        # Assert: escrowed_amount cleared
        await job.arefresh_from_db()
        self.assertEqual(job.escrowed_amount, 0)

    async def test_expired_jobs_count_incremented(self):
        """Ministry term expired_jobs_count should increment."""
        player = await sync_to_async(PlayerFactory)()
        term = await MinistryTerm.objects.acreate(
            minister=player,
            start_date=timezone.now() - timedelta(days=1),
            end_date=timezone.now() + timedelta(days=30),
            initial_budget=Decimal("1000000"),
            current_budget=Decimal("1000000"),
            expired_jobs_count=0,
        )

        await allocate_ministry_budget(1_000_000, term)

        job = await sync_to_async(DeliveryJobFactory)(
            completion_bonus=100_000,
            expired_at=timezone.now() - timedelta(hours=1),
            funding_term=term,
            escrowed_amount=100_000,
        )

        await process_ministry_expiration(job)

        await term.arefresh_from_db()
        self.assertEqual(term.expired_jobs_count, 1)
