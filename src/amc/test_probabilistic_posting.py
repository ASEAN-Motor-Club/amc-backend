"""Tests for probabilistic job posting based on treasury multiplier.

When treasury is low, each slot_to_fill has a treasury_mult chance of
actually being posted. This creates natural stochastic rate reduction
without hard caps.
"""

import itertools
from decimal import Decimal
from unittest.mock import patch, AsyncMock

from django.test import TestCase
from django.utils import timezone

from amc.factories import DeliveryJobTemplateFactory, DeliveryPointFactory
from amc.models import (
    Cargo,
    DeliveryJob,
    DeliveryPointStorage,
    JobPostingConfig,
)
from amc.jobs import monitor_jobs


def _make_template(cargo, source, destination, quantity, name=None):
    return DeliveryJobTemplateFactory(
        name=name or f"Job for {cargo.key}",
        default_quantity=quantity,
        bonus_multiplier=1.0,
        completion_bonus=10_000,
        duration_hours=5,
        job_posting_probability=1.0,
        success_score=1.0,
        cargos=[cargo],
        source_points=[source],
        destination_points=[destination],
    )


_PATCHES = {
    "get_players": "amc.jobs.get_players",
    "announce": "amc.jobs.announce",
    "treasury": "amc.jobs.get_treasury_fund_balance",
    "escrow": "amc.jobs.escrow_ministry_funds",
    "sc_conflicts": "amc.supply_chain.get_conflicting_cargo_keys",
}


class ProbabilisticPostingTestCase(TestCase):
    """Verify that treasury_mult controls the probability of each slot being filled."""

    def setUp(self):
        self.source = DeliveryPointFactory(name="Source Farm")
        self.dest = DeliveryPointFactory(name="Destination")

        self.wheat = Cargo.objects.create(key="C::Wheat", label="Wheat")

        self.template = _make_template(
            self.wheat, self.source, self.dest, quantity=50, name="Wheat Haul"
        )

        DeliveryPointStorage.objects.create(
            delivery_point=self.source,
            cargo=self.wheat,
            cargo_key="C::Wheat",
            kind=DeliveryPointStorage.Kind.OUTPUT,
            amount=200,
            capacity=200,
        )
        DeliveryPointStorage.objects.create(
            delivery_point=self.dest,
            cargo=self.wheat,
            cargo_key="C::Wheat",
            kind=DeliveryPointStorage.Kind.INPUT,
            amount=0,
            capacity=200,
        )

        # 10 slots open, treasury at equilibrium
        JobPostingConfig.objects.update_or_create(
            pk=1,
            defaults={
                "min_base_jobs": 10,
                "max_posts_per_tick": 10,
                "target_success_rate": 0.5,
                "min_multiplier": 1.0,
                "max_multiplier": 2.0,
                "treasury_equilibrium": 50_000_000,
                "treasury_sensitivity": 1.5,
            },
        )

    @patch(_PATCHES["sc_conflicts"], new_callable=AsyncMock, return_value=set())
    @patch(_PATCHES["escrow"], new_callable=AsyncMock, return_value=True)
    @patch(
        _PATCHES["treasury"], new_callable=AsyncMock, return_value=Decimal("50000000")
    )
    @patch(_PATCHES["announce"], new_callable=AsyncMock)
    @patch(
        _PATCHES["get_players"],
        new_callable=AsyncMock,
        return_value=[(1, {"name": "Player1"})] * 5,
    )
    async def test_equilibrium_treasury_posts_all_slots(
        self, mock_players, mock_announce, mock_treasury, mock_escrow, mock_conflicts
    ):
        """At equilibrium (treasury_mult=1.0), every slot rolls random() < 1.0 -> all post."""
        ctx = {"http_client": AsyncMock()}
        await monitor_jobs(ctx)

        job_count = await DeliveryJob.objects.filter(
            fulfilled_at__isnull=True,
            expired_at__gte=timezone.now(),
        ).acount()
        # 1 template -> max 1 job, treasury_mult=1.0 -> always posts
        self.assertEqual(job_count, 1)

    @patch(_PATCHES["sc_conflicts"], new_callable=AsyncMock, return_value=set())
    @patch(_PATCHES["escrow"], new_callable=AsyncMock, return_value=True)
    @patch(_PATCHES["treasury"], new_callable=AsyncMock, return_value=Decimal("50000000"))
    @patch(_PATCHES["announce"], new_callable=AsyncMock)
    @patch(_PATCHES["get_players"], new_callable=AsyncMock, return_value=[(1, {"name": "Player1"})] * 5)
    async def test_low_treasury_probabilistically_skips_slots(
        self, mock_players, mock_announce, mock_treasury, mock_escrow, mock_conflicts
    ):
        """With treasury_mult < 1.0 and random returning 0.6, slots below threshold are skipped."""
        from amc import jobs as jobs_module

        # Set treasury to 25M (50% of 50M equilibrium -> treasury_mult ~ 0.354)
        mock_treasury.return_value = Decimal("25000000")
        # random() returns 0.5 -> 0.5 < 0.354 is False -> slot is skipped
        with patch.object(jobs_module.random, "random", return_value=0.5):
            ctx = {"http_client": AsyncMock()}
            await monitor_jobs(ctx)

        job_count = await DeliveryJob.objects.filter(
            fulfilled_at__isnull=True,
            expired_at__gte=timezone.now(),
        ).acount()
        # random=0.5, treasury_mult~0.354: 0.5 < 0.354 -> False -> no job posted
        self.assertEqual(job_count, 0)

    @patch(_PATCHES["sc_conflicts"], new_callable=AsyncMock, return_value=set())
    @patch(_PATCHES["escrow"], new_callable=AsyncMock, return_value=True)
    @patch(_PATCHES["treasury"], new_callable=AsyncMock, return_value=Decimal("50000000"))
    @patch(_PATCHES["announce"], new_callable=AsyncMock)
    @patch(_PATCHES["get_players"], new_callable=AsyncMock, return_value=[(1, {"name": "Player1"})] * 5)
    async def test_low_treasury_still_posts_when_random_lucky(
        self, mock_players, mock_announce, mock_treasury, mock_escrow, mock_conflicts
    ):
        """Even at low treasury, a lucky roll (random < treasury_mult) still posts."""
        from amc import jobs as jobs_module

        mock_treasury.return_value = Decimal("25000000")
        # random() returns 0.1 -> 0.1 < 0.354 -> True -> slot is filled
        with patch.object(jobs_module.random, "random", return_value=0.1):
            ctx = {"http_client": AsyncMock()}
            await monitor_jobs(ctx)

        job_count = await DeliveryJob.objects.filter(
            fulfilled_at__isnull=True,
            expired_at__gte=timezone.now(),
        ).acount()
        self.assertEqual(job_count, 1)

    @patch(_PATCHES["sc_conflicts"], new_callable=AsyncMock, return_value=set())
    @patch(_PATCHES["escrow"], new_callable=AsyncMock, return_value=True)
    @patch(_PATCHES["treasury"], new_callable=AsyncMock, return_value=Decimal("0"))
    @patch(_PATCHES["announce"], new_callable=AsyncMock)
    @patch(_PATCHES["get_players"], new_callable=AsyncMock, return_value=[(1, {"name": "Player1"})] * 5)
    async def test_zero_treasury_never_posts(
        self, mock_players, mock_announce, mock_treasury, mock_escrow, mock_conflicts
    ):
        """With treasury_mult=0.0, no slot passes (random < 0 is always False)."""
        ctx = {"http_client": AsyncMock()}
        await monitor_jobs(ctx)

        job_count = await DeliveryJob.objects.filter(
            fulfilled_at__isnull=True,
            expired_at__gte=timezone.now(),
        ).acount()
        self.assertEqual(job_count, 0)


class ProbabilisticPostingMultipleSlotsTestCase(TestCase):
    """Test probabilistic behavior with multiple slots available."""

    def setUp(self):
        self.source = DeliveryPointFactory(name="Source Farm")
        self.dest_a = DeliveryPointFactory(name="Destination A")
        self.dest_b = DeliveryPointFactory(name="Destination B")
        self.dest_c = DeliveryPointFactory(name="Destination C")

        self.wheat = Cargo.objects.create(key="C::Wheat", label="Wheat")

        self.template_a = _make_template(
            self.wheat, self.source, self.dest_a, quantity=10, name="Wheat to A"
        )
        self.template_b = _make_template(
            self.wheat, self.source, self.dest_b, quantity=10, name="Wheat to B"
        )
        self.template_c = _make_template(
            self.wheat, self.source, self.dest_c, quantity=10, name="Wheat to C"
        )

        DeliveryPointStorage.objects.create(
            delivery_point=self.source,
            cargo=self.wheat,
            cargo_key="C::Wheat",
            kind=DeliveryPointStorage.Kind.OUTPUT,
            amount=200,
            capacity=200,
        )
        for dest in [self.dest_a, self.dest_b, self.dest_c]:
            DeliveryPointStorage.objects.create(
                delivery_point=dest,
                cargo=self.wheat,
                cargo_key="C::Wheat",
                kind=DeliveryPointStorage.Kind.INPUT,
                amount=0,
                capacity=200,
            )

        JobPostingConfig.objects.update_or_create(
            pk=1,
            defaults={
                "min_base_jobs": 10,
                "max_posts_per_tick": 10,
                "target_success_rate": 0.5,
                "min_multiplier": 1.0,
                "max_multiplier": 2.0,
                "treasury_equilibrium": 50_000_000,
                "treasury_sensitivity": 1.5,
            },
        )

    @patch(_PATCHES["sc_conflicts"], new_callable=AsyncMock, return_value=set())
    @patch(_PATCHES["escrow"], new_callable=AsyncMock, return_value=True)
    @patch(_PATCHES["treasury"], new_callable=AsyncMock, return_value=Decimal("50000000"))
    @patch(_PATCHES["announce"], new_callable=AsyncMock)
    @patch(_PATCHES["get_players"], new_callable=AsyncMock, return_value=[(1, {"name": "Player1"})] * 5)
    async def test_multiple_slots_probabilistic_distribution(
        self, mock_players, mock_announce, mock_treasury, mock_escrow, mock_conflicts
    ):
        """With 3 templates and treasury_mult=0.5, controlled random gives 2 of 3 slots."""
        from amc import jobs as jobs_module

        with patch.object(
            jobs_module, "calculate_treasury_multiplier", return_value=0.5
        ):
            # Probability gate values: 0.1 < 0.5 pass, 0.8 >= 0.5 fail, 0.3 < 0.5 pass
            # Pad with 0.5 for weighted_shuffle/bonus calls after the gate
            prob_values = [0.1, 0.8, 0.3]
            shuffle_pad = [0.5] * 20
            controlled = iter(prob_values + shuffle_pad)
            with patch.object(
                jobs_module.random,
                "random",
                side_effect=itertools.chain(controlled, itertools.repeat(0.5)),
            ):
                ctx = {"http_client": AsyncMock()}
                await monitor_jobs(ctx)

        job_count = await DeliveryJob.objects.filter(
            fulfilled_at__isnull=True,
            expired_at__gte=timezone.now(),
        ).acount()
        self.assertEqual(job_count, 2)

    @patch(_PATCHES["sc_conflicts"], new_callable=AsyncMock, return_value=set())
    @patch(_PATCHES["escrow"], new_callable=AsyncMock, return_value=True)
    @patch(_PATCHES["treasury"], new_callable=AsyncMock, return_value=Decimal("50000000"))
    @patch(_PATCHES["announce"], new_callable=AsyncMock)
    @patch(_PATCHES["get_players"], new_callable=AsyncMock, return_value=[(1, {"name": "Player1"})] * 5)
    async def test_all_lucky_at_moderate_treasury(
        self, mock_players, mock_announce, mock_treasury, mock_escrow, mock_conflicts
    ):
        """At treasury_mult=0.5, all lucky rolls (all < 0.5) post all slots."""
        from amc import jobs as jobs_module

        with patch.object(
            jobs_module, "calculate_treasury_multiplier", return_value=0.5
        ):
            # All probability gate values < 0.5 -> all pass
            prob_values = [0.1, 0.2, 0.3]
            shuffle_pad = [0.5] * 20
            controlled = iter(prob_values + shuffle_pad)
            with patch.object(
                jobs_module.random,
                "random",
                side_effect=itertools.chain(controlled, itertools.repeat(0.5)),
            ):
                ctx = {"http_client": AsyncMock()}
                await monitor_jobs(ctx)

        job_count = await DeliveryJob.objects.filter(
            fulfilled_at__isnull=True,
            expired_at__gte=timezone.now(),
        ).acount()
        self.assertEqual(job_count, 3)

    @patch(_PATCHES["sc_conflicts"], new_callable=AsyncMock, return_value=set())
    @patch(_PATCHES["escrow"], new_callable=AsyncMock, return_value=True)
    @patch(_PATCHES["treasury"], new_callable=AsyncMock, return_value=Decimal("50000000"))
    @patch(_PATCHES["announce"], new_callable=AsyncMock)
    @patch(_PATCHES["get_players"], new_callable=AsyncMock, return_value=[(1, {"name": "Player1"})] * 5)
    async def test_all_unlucky_at_moderate_treasury(
        self, mock_players, mock_announce, mock_treasury, mock_escrow, mock_conflicts
    ):
        """At treasury_mult=0.5, all unlucky rolls (all > 0.5) skip all slots."""
        from amc import jobs as jobs_module

        with patch.object(
            jobs_module, "calculate_treasury_multiplier", return_value=0.5
        ):
            # All probability gate values > 0.5 -> all fail, returns early
            prob_values = [0.8, 0.9, 0.95]
            controlled = iter(prob_values)
            with patch.object(
                jobs_module.random,
                "random",
                side_effect=itertools.chain(controlled, itertools.repeat(0.99)),
            ):
                ctx = {"http_client": AsyncMock()}
                await monitor_jobs(ctx)

        job_count = await DeliveryJob.objects.filter(
            fulfilled_at__isnull=True,
            expired_at__gte=timezone.now(),
        ).acount()
        self.assertEqual(job_count, 0)

    @patch(_PATCHES["sc_conflicts"], new_callable=AsyncMock, return_value=set())
    @patch(_PATCHES["escrow"], new_callable=AsyncMock, return_value=True)
    @patch(_PATCHES["treasury"], new_callable=AsyncMock, return_value=Decimal("50000000"))
    @patch(_PATCHES["announce"], new_callable=AsyncMock)
    @patch(_PATCHES["get_players"], new_callable=AsyncMock, return_value=[])
    async def test_min_active_jobs_floor_at_low_player_count(
        self, mock_players, mock_announce, mock_treasury, mock_escrow, mock_conflicts
    ):
        """With 0 players and a depressed adaptive multiplier, the board would
        otherwise collapse to ~1 job; the min_active_jobs floor keeps 3 up."""
        from amc import jobs as jobs_module

        # Tune so the log2 curve + adaptive mult naturally yields ~1 at 0 players.
        # Use the async variant: this test body runs in async context, and a
        # sync update_or_create would raise SynchronousOnlyOperation.
        await JobPostingConfig.objects.aupdate_or_create(
            pk=1,
            defaults={
                "min_base_jobs": 1,
                "min_active_jobs": 3,
                "max_posts_per_tick": 10,
                "max_multiplier": 2.0,
                "treasury_equilibrium": 50_000_000,
                "treasury_sensitivity": 1.5,
            },
        )

        with patch.object(
            jobs_module, "calculate_adaptive_multiplier", return_value=0.5
        ):
            # treasury_mult=1.0 -> posting_chance 1.0 -> every open slot posts
            with patch.object(
                jobs_module, "calculate_treasury_multiplier", return_value=1.0
            ):
                ctx = {"http_client": AsyncMock()}
                await monitor_jobs(ctx)

        job_count = await DeliveryJob.objects.filter(
            fulfilled_at__isnull=True,
            expired_at__gte=timezone.now(),
        ).acount()
        self.assertEqual(job_count, 3)
