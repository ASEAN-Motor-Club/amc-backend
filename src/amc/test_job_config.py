from django.test import TestCase
from amc.models import JobPostingConfig


class JobPostingConfigTestCase(TestCase):
    """Tests for the JobPostingConfig singleton model."""

    async def test_aget_config_creates_default(self):
        """aget_config should create a default config if none exists."""
        config = await JobPostingConfig.aget_config()
        self.assertEqual(config.pk, 1)
        self.assertEqual(config.target_success_rate, 0.50)
        self.assertEqual(config.min_multiplier, 0.5)
        self.assertEqual(config.max_multiplier, 1.5)
        self.assertEqual(config.players_per_job, 10)
        self.assertEqual(config.min_base_jobs, 1)
        self.assertEqual(config.min_active_jobs, 3)
        self.assertEqual(config.posting_rate_multiplier, 1.0)

    async def test_aget_config_returns_existing(self):
        """aget_config should return the existing config, not create a new one."""
        await JobPostingConfig.objects.acreate(pk=1, posting_rate_multiplier=3.0)
        config = await JobPostingConfig.aget_config()
        self.assertEqual(config.pk, 1)
        self.assertEqual(config.posting_rate_multiplier, 3.0)

    async def test_singleton_enforced(self):
        """Saving always uses pk=1 regardless of what pk is set."""
        config = JobPostingConfig(posting_rate_multiplier=2.5)
        await config.asave()
        self.assertEqual(config.pk, 1)

        # Saving a second instance should overwrite, not create
        config2 = JobPostingConfig(posting_rate_multiplier=0.5)
        await config2.asave()
        self.assertEqual(config2.pk, 1)

        count = await JobPostingConfig.objects.acount()
        self.assertEqual(count, 1)

        latest = await JobPostingConfig.aget_config()
        self.assertEqual(latest.posting_rate_multiplier, 0.5)

    async def test_delete_prevented(self):
        """Deleting the singleton should be a no-op."""
        config = await JobPostingConfig.aget_config()
        config.delete()  # Should be a no-op
        count = await JobPostingConfig.objects.acount()
        self.assertEqual(count, 1)
