from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from asgiref.sync import sync_to_async
from amc.factories import DeliveryJobFactory
from amc.jobs import get_job_success_rate, calculate_adaptive_multiplier


class AdaptiveMultiplierTestCase(TestCase):
    """Tests for the calculate_adaptive_multiplier function."""

    def test_multiplier_at_target_rate(self):
        """80% success rate should give multiplier of 1.0."""
        multiplier = calculate_adaptive_multiplier(0.80)
        self.assertAlmostEqual(multiplier, 1.0, places=2)

    def test_multiplier_at_100_percent(self):
        """100% success rate should give max multiplier of 2.0."""
        multiplier = calculate_adaptive_multiplier(1.0)
        self.assertAlmostEqual(multiplier, 2.0, places=2)

    def test_multiplier_at_0_percent(self):
        """0% success rate should give min multiplier of 0.5."""
        multiplier = calculate_adaptive_multiplier(0.0)
        self.assertAlmostEqual(multiplier, 0.5, places=2)

    def test_multiplier_at_40_percent(self):
        """40% success rate (half of target) should give ~0.75."""
        multiplier = calculate_adaptive_multiplier(0.40)
        # 0.5 + (0.40/0.80) * (1.0 - 0.5) = 0.5 + 0.5 * 0.5 = 0.75
        self.assertAlmostEqual(multiplier, 0.75, places=2)

    def test_multiplier_at_90_percent(self):
        """90% success rate should give ~1.5."""
        multiplier = calculate_adaptive_multiplier(0.90)
        # (0.90 - 0.80) / (1.0 - 0.80) = 0.10 / 0.20 = 0.5
        # 1.0 + 0.5 * (2.0 - 1.0) = 1.5
        self.assertAlmostEqual(multiplier, 1.5, places=2)


class JobSuccessRateTestCase(TestCase):
    """Tests for get_job_success_rate function."""

    async def test_success_rate_no_data(self):
        """No recent jobs should return success rate of 1.0."""
        rate, completed, expired = await get_job_success_rate(hours_lookback=24)
        self.assertEqual(rate, 1.0)
        self.assertEqual(completed, 0)
        self.assertEqual(expired, 0)

    async def test_success_rate_all_completed(self):
        """All jobs completed should return 100% success rate."""
        now = timezone.now()
        # Create 3 completed jobs within lookback
        for _ in range(3):
            await sync_to_async(DeliveryJobFactory)(
                fulfilled_at=now - timedelta(hours=2),
                expired_at=now + timedelta(hours=1),  # Not expired
            )

        rate, completed, expired = await get_job_success_rate(hours_lookback=24)
        self.assertEqual(rate, 1.0)
        self.assertEqual(completed, 3)
        self.assertEqual(expired, 0)

    async def test_success_rate_all_expired(self):
        """All jobs expired should return 0% success rate."""
        now = timezone.now()
        # Create 2 expired, unfulfilled jobs within lookback
        for _ in range(2):
            await sync_to_async(DeliveryJobFactory)(
                fulfilled_at=None,
                expired_at=now - timedelta(hours=2),  # Expired
            )

        rate, completed, expired = await get_job_success_rate(hours_lookback=24)
        self.assertEqual(rate, 0.0)
        self.assertEqual(completed, 0)
        self.assertEqual(expired, 2)

    async def test_success_rate_mixed(self):
        """Mix of completed and expired should return proportional rate."""
        now = timezone.now()
        # Create 3 completed
        for _ in range(3):
            await sync_to_async(DeliveryJobFactory)(
                fulfilled_at=now - timedelta(hours=2),
                expired_at=now + timedelta(hours=1),
            )
        # Create 1 expired
        await sync_to_async(DeliveryJobFactory)(
            fulfilled_at=None,
            expired_at=now - timedelta(hours=2),
        )

        rate, completed, expired = await get_job_success_rate(hours_lookback=24)
        self.assertAlmostEqual(rate, 0.75, places=2)  # 3/4
        self.assertEqual(completed, 3)
        self.assertEqual(expired, 1)

    async def test_success_rate_excludes_old_data(self):
        """Jobs outside lookback window should be excluded."""
        now = timezone.now()
        # Create job completed 48 hours ago (outside 24h lookback)
        await sync_to_async(DeliveryJobFactory)(
            fulfilled_at=now - timedelta(hours=48),
            expired_at=now - timedelta(hours=42),
        )
        # Create job expired 48 hours ago
        await sync_to_async(DeliveryJobFactory)(
            fulfilled_at=None,
            expired_at=now - timedelta(hours=48),
        )

        rate, completed, expired = await get_job_success_rate(hours_lookback=24)
        self.assertEqual(rate, 1.0)  # No data in window, defaults to healthy
        self.assertEqual(completed, 0)
        self.assertEqual(expired, 0)
