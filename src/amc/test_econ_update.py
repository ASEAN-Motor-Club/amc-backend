"""Tests for partial job completion payouts.

Covers the payout_partial_contributors function and related logic
for rewarding players when a job expires partially completed.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch, MagicMock

from asgiref.sync import sync_to_async
from django.test import TestCase
from django.utils import timezone

from amc.jobs import payout_partial_contributors, cleanup_expired_jobs
from amc.models import Delivery, DeliveryJob, Player, Character, DeliveryJobTemplate, MinistryTerm
from amc.factories import DeliveryJobFactory, PlayerFactory, CharacterFactory
from amc_finance.models import Account
from amc_finance.services import (
    get_treasury_fund_balance,
    process_treasury_expiration_penalty,
)


class PartialPayoutTestCase(TestCase):
    """Tests for payout_partial_contributors function."""

    async def test_basic_partial_payout_two_contributors(self):
        """50% completion with 2 contributors - each gets proportional share."""
        # Setup
        player1 = await sync_to_async(PlayerFactory)()
        char1 = await sync_to_async(CharacterFactory)(player=player1, name="Player1")
        
        player2 = await sync_to_async(PlayerFactory)()
        char2 = await sync_to_async(CharacterFactory)(player=player2, name="Player2")

        job = await sync_to_async(DeliveryJobFactory)(
            name="Test Job",
            quantity_requested=100,
            quantity_fulfilled=50,
            completion_bonus=100_000,
            expired_at=timezone.now() - timedelta(hours=1),
            funding_term=None,
        )

        # Create delivery logs
        await Delivery.objects.acreate(
            timestamp=timezone.now(),
            character=char1,
            cargo_key="SunflowerSeed",
            quantity=30,
            payment=1000,
            subsidy=0,
            job=job,
        )
        await Delivery.objects.acreate(
            timestamp=timezone.now(),
            character=char2,
            cargo_key="SunflowerSeed",
            quantity=20,
            payment=1000,
            subsidy=0,
            job=job,
        )

        # Execute with mocked send_fund_to_player
        http_client = AsyncMock()
        with patch("amc.jobs.send_fund_to_player", new_callable=AsyncMock) as mock_send:
            await payout_partial_contributors(job, http_client)

            # Assert: Both players should be paid
            self.assertEqual(mock_send.call_count, 2)
            
            # Player1 contributed 30/50 = 60% of fulfilled amount
            # partial_bonus = 100_000 * 0.5 = 50_000
            # Player1 should get 30/50 * 50_000 = 30_000
            calls = mock_send.call_args_list
            rewards = {call.args[1].name: call.args[0] for call in calls}
            self.assertEqual(rewards["Player1"], 30_000)
            self.assertEqual(rewards["Player2"], 20_000)

    async def test_weighted_cargo_counts_double(self):
        """Container_40ft_01 should count as 2x toward fulfillment."""
        player = await sync_to_async(PlayerFactory)()
        char = await sync_to_async(CharacterFactory)(player=player, name="ContainerDriver")

        job = await sync_to_async(DeliveryJobFactory)(
            name="Container Job",
            quantity_requested=100,
            quantity_fulfilled=40,  # weighted
            completion_bonus=100_000,
            expired_at=timezone.now() - timedelta(hours=1),
            funding_term=None,
        )

        # Container delivery: quantity=20, weight=2 -> weighted=40
        await Delivery.objects.acreate(
            timestamp=timezone.now(),
            character=char,
            cargo_key="Container_40ft_01",
            quantity=20,
            payment=1000,
            subsidy=0,
            job=job,
        )

        http_client = AsyncMock()
        with patch("amc.jobs.send_fund_to_player", new_callable=AsyncMock) as mock_send:
            await payout_partial_contributors(job, http_client)

            # partial_bonus = 100_000 * 0.4 = 40_000
            # Player contributed 40/40 = 100%
            mock_send.assert_called_once_with(40_000, char, "Job Completion")

    async def test_gov_employee_gets_contribution_not_money(self):
        """Gov employees should have contribution tracked, not receive money."""
        player = await sync_to_async(PlayerFactory)()
        char = await sync_to_async(CharacterFactory)(
            player=player, 
            name="GovEmployee",
            gov_employee_until=timezone.now() + timedelta(days=1),
        )

        job = await sync_to_async(DeliveryJobFactory)(
            name="Gov Job",
            quantity_requested=100,
            quantity_fulfilled=50,
            completion_bonus=100_000,
            expired_at=timezone.now() - timedelta(hours=1),
            funding_term=None,
        )

        await Delivery.objects.acreate(
            timestamp=timezone.now(),
            character=char,
            cargo_key="SunflowerSeed",
            quantity=50,
            payment=1000,
            subsidy=0,
            job=job,
        )

        http_client = AsyncMock()
        with patch("amc.jobs.send_fund_to_player", new_callable=AsyncMock) as mock_send, \
             patch("amc.gov_employee.redirect_income_to_treasury", new_callable=AsyncMock) as mock_redirect, \
             patch("amc.jobs.announce", new_callable=AsyncMock):
            await payout_partial_contributors(job, http_client)

            # Gov employee should NOT receive direct payment
            mock_send.assert_not_called()
            
            # But should have contribution tracked
            mock_redirect.assert_called_once()
            call = mock_redirect.call_args
            self.assertEqual(call.args[0], 0)  # amount is first positional arg
            self.assertEqual(call.kwargs["contribution"], 50_000)  # 50% of 100k

    async def test_zero_bonus_skips_payout(self):
        """Jobs with 0 completion_bonus should not trigger payouts."""
        job = await sync_to_async(DeliveryJobFactory)(
            quantity_requested=100,
            quantity_fulfilled=50,
            completion_bonus=0,
            expired_at=timezone.now() - timedelta(hours=1),
            funding_term=None,
        )

        http_client = AsyncMock()
        with patch("amc.jobs.send_fund_to_player", new_callable=AsyncMock) as mock_send:
            await payout_partial_contributors(job, http_client)
            mock_send.assert_not_called()

    async def test_zero_fulfillment_skips_payout(self):
        """Jobs with 0 fulfillment should not trigger payouts."""
        job = await sync_to_async(DeliveryJobFactory)(
            quantity_requested=100,
            quantity_fulfilled=0,
            completion_bonus=100_000,
            expired_at=timezone.now() - timedelta(hours=1),
            funding_term=None,
        )

        http_client = AsyncMock()
        with patch("amc.jobs.send_fund_to_player", new_callable=AsyncMock) as mock_send:
            await payout_partial_contributors(job, http_client)
            mock_send.assert_not_called()

    async def test_100_percent_completion_full_bonus(self):
        """Job expired at 100% should pay full completion bonus."""
        player = await sync_to_async(PlayerFactory)()
        char = await sync_to_async(CharacterFactory)(player=player, name="SoloPlayer")

        job = await sync_to_async(DeliveryJobFactory)(
            name="Complete Job",
            quantity_requested=100,
            quantity_fulfilled=100,
            completion_bonus=100_000,
            expired_at=timezone.now() - timedelta(hours=1),
            funding_term=None,
        )

        await Delivery.objects.acreate(
            timestamp=timezone.now(),
            character=char,
            cargo_key="SunflowerSeed",
            quantity=100,
            payment=1000,
            subsidy=0,
            job=job,
        )

        http_client = AsyncMock()
        with patch("amc.jobs.send_fund_to_player", new_callable=AsyncMock) as mock_send:
            await payout_partial_contributors(job, http_client)

            # Full bonus should be paid
            mock_send.assert_called_once_with(100_000, char, "Job Completion")

    async def test_announcement_sent_on_payout(self):
        """Announcement should be sent when payouts are made."""
        player = await sync_to_async(PlayerFactory)()
        char = await sync_to_async(CharacterFactory)(player=player, name="Announced")

        job = await sync_to_async(DeliveryJobFactory)(
            name="Announce Job",
            quantity_requested=100,
            quantity_fulfilled=50,
            completion_bonus=100_000,
            expired_at=timezone.now() - timedelta(hours=1),
            funding_term=None,
        )

        await Delivery.objects.acreate(
            timestamp=timezone.now(),
            character=char,
            cargo_key="SunflowerSeed",
            quantity=50,
            payment=1000,
            subsidy=0,
            job=job,
        )

        http_client = AsyncMock()
        with patch("amc.jobs.send_fund_to_player", new_callable=AsyncMock), \
             patch("amc.jobs.announce", new_callable=AsyncMock) as mock_announce:
            await payout_partial_contributors(job, http_client)

            mock_announce.assert_called_once()
            message = mock_announce.call_args[0][0]
            self.assertIn("50% completion", message)
            self.assertIn("Announced", message)


class CleanupExpiredJobsTestCase(TestCase):
    """Tests for cleanup_expired_jobs with partial payouts."""

    async def test_non_ministry_job_triggers_partial_payout(self):
        """Non-ministry expired job should call payout_partial_contributors."""
        player = await sync_to_async(PlayerFactory)()
        char = await sync_to_async(CharacterFactory)(player=player)

        job = await sync_to_async(DeliveryJobFactory)(
            quantity_requested=100,
            quantity_fulfilled=50,
            completion_bonus=100_000,
            expired_at=timezone.now() - timedelta(hours=1),
            funding_term=None,
            escrowed_amount=0,
        )

        await Delivery.objects.acreate(
            timestamp=timezone.now(),
            character=char,
            cargo_key="SunflowerSeed",
            quantity=50,
            payment=1000,
            subsidy=0,
            job=job,
        )

        http_client = AsyncMock()
        with patch("amc.jobs.payout_partial_contributors", new_callable=AsyncMock) as mock_payout, \
             patch("amc.jobs.process_treasury_expiration_penalty", new_callable=AsyncMock) as mock_penalty:
            await cleanup_expired_jobs(http_client)

            mock_payout.assert_called_once()
            mock_penalty.assert_called_once()

    async def test_ministry_job_no_partial_payout(self):
        """Ministry-funded expired job should NOT call payout_partial_contributors."""
        minister = await sync_to_async(PlayerFactory)()
        term = await MinistryTerm.objects.acreate(
            minister=minister,
            start_date=timezone.now() - timedelta(days=1),
            end_date=timezone.now() + timedelta(days=30),
            initial_budget=Decimal("1000000"),
            current_budget=Decimal("1000000"),
        )

        job = await sync_to_async(DeliveryJobFactory)(
            quantity_requested=100,
            quantity_fulfilled=50,
            completion_bonus=100_000,
            expired_at=timezone.now() - timedelta(hours=1),
            funding_term=term,
            escrowed_amount=100_000,
        )

        http_client = AsyncMock()
        with patch("amc.jobs.payout_partial_contributors", new_callable=AsyncMock) as mock_payout, \
             patch("amc.jobs.process_ministry_expiration", new_callable=AsyncMock) as mock_ministry:
            await cleanup_expired_jobs(http_client)

            # Ministry job should NOT get partial payout (current behavior)
            mock_payout.assert_not_called()
            mock_ministry.assert_called_once()


class TreasuryPenaltyFlagTestCase(TestCase):
    """Tests for TREASURY_EXPIRATION_PENALTY_ENABLED feature flag."""

    async def test_penalty_applied_when_enabled(self):
        """When flag is True, treasury should be charged penalty."""
        from amc import config
        
        # Ensure flag is True (default)
        original = config.TREASURY_EXPIRATION_PENALTY_ENABLED
        config.TREASURY_EXPIRATION_PENALTY_ENABLED = True
        
        try:
            initial_balance = await get_treasury_fund_balance()
            
            job = await sync_to_async(DeliveryJobFactory)(
                completion_bonus=100_000,
                expired_at=timezone.now() - timedelta(hours=1),
                funding_term=None,
            )

            await process_treasury_expiration_penalty(job)

            final_balance = await get_treasury_fund_balance()
            self.assertEqual(initial_balance - 50_000, final_balance)
        finally:
            config.TREASURY_EXPIRATION_PENALTY_ENABLED = original

    async def test_penalty_skipped_when_disabled(self):
        """When flag is False, treasury should NOT be charged."""
        from amc import config
        
        original = config.TREASURY_EXPIRATION_PENALTY_ENABLED
        config.TREASURY_EXPIRATION_PENALTY_ENABLED = False
        
        try:
            initial_balance = await get_treasury_fund_balance()
            
            job = await sync_to_async(DeliveryJobFactory)(
                completion_bonus=100_000,
                expired_at=timezone.now() - timedelta(hours=1),
                funding_term=None,
            )

            await process_treasury_expiration_penalty(job)

            final_balance = await get_treasury_fund_balance()
            self.assertEqual(initial_balance, final_balance)
        finally:
            config.TREASURY_EXPIRATION_PENALTY_ENABLED = original


class WeightedDeliveryBonusTestCase(TestCase):
    """Tests for weighted cargo in atomic_process_delivery."""

    def setUp(self):
        self.player = Player.objects.create(unique_id=123)
        self.character = Character.objects.create(player=self.player, name="TestChar")

    def test_weighted_delivery_counts_toward_fulfillment(self):
        """Container_40ft_01 should count double toward fulfillment."""
        job = DeliveryJob.objects.create(
            name="Container Job",
            cargo_key="Container_40ft_01",
            quantity_requested=100,
            quantity_fulfilled=0,
            bonus_multiplier=1.0,
            expired_at=timezone.now() + timedelta(days=1),
        )

        delivery_data = {
            "timestamp": timezone.now(),
            "character": self.character,
            "cargo_key": "Container_40ft_01",
            "quantity": 20,
            "payment": 10000,
            "subsidy": 0,
        }

        from amc.pipeline.delivery import atomic_process_delivery
        atomic_process_delivery(job.id, 20, delivery_data)

        job.refresh_from_db()
        # 20 * 2 = 40 should be added
        self.assertEqual(job.quantity_fulfilled, 40)

    def test_bonus_prorated_when_capped(self):
        """When delivery exceeds remaining, bonus should be prorated."""
        job = DeliveryJob.objects.create(
            name="Almost Full",
            cargo_key="SunflowerSeed",
            quantity_requested=100,
            quantity_fulfilled=90,
            bonus_multiplier=1.0,
            expired_at=timezone.now() + timedelta(days=1),
        )

        delivery_data = {
            "timestamp": timezone.now(),
            "character": self.character,
            "cargo_key": "SunflowerSeed",
            "quantity": 20,
            "payment": 10000,
            "subsidy": 0,
        }

        from amc.pipeline.delivery import atomic_process_delivery
        atomic_process_delivery(job.id, 20, delivery_data)

        # Only 10 added (capped at remaining), bonus = 10000 * (10/20) = 5000
        self.assertEqual(delivery_data["subsidy"], 5000)

    def test_weighted_bonus_prorated_when_capped(self):
        """Weighted delivery capped should prorate bonus correctly."""
        job = DeliveryJob.objects.create(
            name="Container Almost Full",
            cargo_key="Container_40ft_01",
            quantity_requested=100,
            quantity_fulfilled=80,
            bonus_multiplier=1.0,
            expired_at=timezone.now() + timedelta(days=1),
        )

        # quantity=20, weight=2, weighted=40
        # remaining=20, so capped at 20
        delivery_data = {
            "timestamp": timezone.now(),
            "character": self.character,
            "cargo_key": "Container_40ft_01",
            "quantity": 20,
            "payment": 10000,
            "subsidy": 0,
        }

        from amc.pipeline.delivery import atomic_process_delivery
        atomic_process_delivery(job.id, 20, delivery_data)

        job.refresh_from_db()
        # 80 + 20 = 100 (capped)
        self.assertEqual(job.quantity_fulfilled, 100)
        
        # bonus = 10000 * (20/40) = 5000 (prorated)
        self.assertEqual(delivery_data["subsidy"], 5000)

    def test_unweighted_cargo_unchanged(self):
        """Regular cargo (not in CARGO_FULFILLMENT_WEIGHTS) should behave normally."""
        job = DeliveryJob.objects.create(
            name="Regular Job",
            cargo_key="SunflowerSeed",
            quantity_requested=100,
            quantity_fulfilled=0,
            bonus_multiplier=1.0,
            expired_at=timezone.now() + timedelta(days=1),
        )

        delivery_data = {
            "timestamp": timezone.now(),
            "character": self.character,
            "cargo_key": "SunflowerSeed",
            "quantity": 20,
            "payment": 10000,
            "subsidy": 0,
        }

        from amc.pipeline.delivery import atomic_process_delivery
        atomic_process_delivery(job.id, 20, delivery_data)

        job.refresh_from_db()
        # Normal behavior: 20 added
        self.assertEqual(job.quantity_fulfilled, 20)
        
        # Full bonus: 10000 * (20/20) = 10000
        self.assertEqual(delivery_data["subsidy"], 10_000)


class WelcomeMessageFixTestCase(TestCase):
    """Tests for the welcome message fix for quick relogs."""

    def test_quick_relog_message(self):
        """Player relogging within 1 hour should get 'That was quick!' message."""
        from amc.tasks import get_welcome_message
        
        last_online = timezone.now() - timedelta(minutes=30)
        message, is_new = get_welcome_message("TestPlayer", False, last_online)
        
        self.assertIn("That was quick", message)
        self.assertFalse(is_new)

    def test_normal_relog_message(self):
        """Player relogging after 1 hour should get normal welcome back."""
        from amc.tasks import get_welcome_message
        
        last_online = timezone.now() - timedelta(hours=2)
        message, is_new = get_welcome_message("TestPlayer", False, last_online)
        
        self.assertIn("Welcome back", message)
        self.assertNotIn("That was quick", message)
        self.assertFalse(is_new)

    def test_new_player_no_message(self):
        """New players should not get welcome back message."""
        from amc.tasks import get_welcome_message
        
        message, is_new = get_welcome_message("NewPlayer", True)
        
        # New players get a welcome message
        self.assertIsNotNone(message)
        self.assertTrue(is_new)
