from django.test import TestCase
from django.utils import timezone
from datetime import timedelta
from amc.models import MinistryTerm, Player, DeliveryJob, DeliveryJobTemplate
from amc_finance.models import Account
from amc_finance.services import (
    allocate_ministry_budget,
    escrow_ministry_funds,
    process_ministry_completion,
    process_ministry_expiration,
)
from amc.deliverypoints import monitor_jobs, cleanup_expired_jobs

class MinistryFinanceTestCase(TestCase):
    async def test_financial_workflow(self):
        # 1. Setup
        player = await Player.objects.acreate(unique_id=12345, discord_name="Test Minister")
        term = await MinistryTerm.objects.acreate(
            minister=player,
            start_date=timezone.now(),
            end_date=timezone.now() + timedelta(days=7),
            initial_budget=50_000_000,
            current_budget=0, # Will be set by allocate
        )

        # 2. Allocation
        await allocate_ministry_budget(50_000_000, term)
        await term.arefresh_from_db()
        self.assertEqual(term.current_budget, 50_000_000)
        
        ministry_budget_acc = await Account.objects.aget(name='Ministry of Commerce Budget')
        self.assertEqual(ministry_budget_acc.balance, 50_000_000)
        
        # 3. Job Creation & Escrow
        job_template = await DeliveryJobTemplate.objects.acreate(
            name="Test Job Template",
            default_quantity=100,
            bonus_multiplier=1.0,
            completion_bonus=1_000_000
        )
        
        # Test monitor_jobs logic (mocking or calling it might be complex due to random, so we test direct logic)
        # Verify manual escrow
        job = await DeliveryJob.objects.acreate(
            name="Test Job",
            quantity_requested=100,
            bonus_multiplier=1.0,
            completion_bonus=1_000_000,
            funding_term=term,
            created_from=job_template
        )
        
        success = await escrow_ministry_funds(1_000_000, job)
        self.assertTrue(success)
        job.escrowed_amount = 1_000_000
        await job.asave()
        
        await term.arefresh_from_db()
        self.assertEqual(term.current_budget, 49_000_000)
        
        ministry_escrow_acc = await Account.objects.aget(name='Ministry of Commerce Escrow')
        self.assertEqual(ministry_escrow_acc.balance, 1_000_000)
        
        # 4. Completion (Rebate)
        await process_ministry_completion(job, 1_000_000)
        
        await term.arefresh_from_db()
        # Budget = 49M + (20% of 1M = 200k) = 49,200,000
        self.assertEqual(term.current_budget, 49_200_000)
        
        await ministry_escrow_acc.arefresh_from_db()
        self.assertEqual(ministry_escrow_acc.balance, 0)
        
        ministry_expense_acc = await Account.objects.aget(name='Ministry of Commerce Expenses')
        self.assertEqual(ministry_expense_acc.balance, 1_000_000)
        
        # 5. Expiration (Refund)
        # Create another job for expiration
        job_expired = await DeliveryJob.objects.acreate(
            name="Expired Job",
            quantity_requested=100,
            bonus_multiplier=1.0,
            completion_bonus=2_000_000,
            funding_term=term,
            created_from=job_template
        )
        success = await escrow_ministry_funds(2_000_000, job_expired)
        self.assertTrue(success)
        job_expired.escrowed_amount = 2_000_000
        await job_expired.asave()
        
        await term.arefresh_from_db()
        self.assertEqual(term.current_budget, 47_200_000) # 49.2M - 2M
        
        # Trigger cleanup
        job_expired.expired_at = timezone.now() - timedelta(hours=1)
        await job_expired.asave()
        
        await cleanup_expired_jobs()
        
        await term.arefresh_from_db()
        # Refund = 50% of 2M = 1M.
        # Budget = 47.2M + 1M = 48.2M
        self.assertEqual(term.current_budget, 48_200_000)
        
        await ministry_escrow_acc.arefresh_from_db()
        self.assertEqual(ministry_escrow_acc.balance, 0)
        
        await ministry_expense_acc.arefresh_from_db()
        # Expenses = 1M (previous) + 1M (burned) = 2M
        self.assertEqual(ministry_expense_acc.balance, 2_000_000)
        
        await job_expired.arefresh_from_db()
        self.assertEqual(job_expired.escrowed_amount, 0)
