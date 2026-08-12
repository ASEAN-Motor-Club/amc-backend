from datetime import timedelta
from unittest.mock import AsyncMock

from django.test import TestCase
from django.utils import timezone

from amc.models import ScheduledAnnouncement
from amc.pinned_announcement import (
    current_pinned_message,
    drive_pinned_announcement,
    announce_server_restart,
    RESTART_MESSAGE,
)


class ScheduledAnnouncementModelTestCase(TestCase):
    async def test_one_shot_not_yet_live(self):
        entry = await ScheduledAnnouncement.objects.acreate(
            message="future", scheduled_at=timezone.now() + timedelta(hours=1)
        )
        self.assertIsNone(entry.latest_occurrence(timezone.now()))

    async def test_one_shot_live(self):
        when = timezone.now() - timedelta(hours=1)
        entry = await ScheduledAnnouncement.objects.acreate(
            message="past", scheduled_at=when
        )
        self.assertEqual(entry.latest_occurrence(timezone.now()), when)

    async def test_weekly_recurrence_advances(self):
        first = timezone.now() - timedelta(weeks=3)
        entry = await ScheduledAnnouncement.objects.acreate(
            message="weekly",
            scheduled_at=first,
            repeat=ScheduledAnnouncement.Repeat.WEEKLY,
        )
        occ = entry.latest_occurrence(timezone.now())
        self.assertGreaterEqual(occ, timezone.now() - timedelta(weeks=1))
        self.assertLessEqual(occ, timezone.now())

    async def test_weekly_window_lapses(self):
        first = timezone.now() - timedelta(weeks=1, minutes=30)
        entry = await ScheduledAnnouncement.objects.acreate(
            message="windowed",
            scheduled_at=first,
            repeat=ScheduledAnnouncement.Repeat.WEEKLY,
            active_minutes=1,
        )
        self.assertIsNone(entry.latest_occurrence(timezone.now()))

    async def test_weekly_window_active(self):
        first = timezone.now() - timedelta(weeks=1, minutes=30)
        entry = await ScheduledAnnouncement.objects.acreate(
            message="windowed",
            scheduled_at=first,
            repeat=ScheduledAnnouncement.Repeat.WEEKLY,
            active_minutes=60,
        )
        self.assertIsNotNone(entry.latest_occurrence(timezone.now()))

    async def test_daily_advance(self):
        first = timezone.now() - timedelta(days=5)
        entry = await ScheduledAnnouncement.objects.acreate(
            message="daily",
            scheduled_at=first,
            repeat=ScheduledAnnouncement.Repeat.DAILY,
        )
        occ = entry.latest_occurrence(timezone.now())
        self.assertGreaterEqual(occ, timezone.now() - timedelta(days=1))
        self.assertLessEqual(occ, timezone.now())

    async def test_monthly_day_clamp_does_not_drift(self):
        jan_31 = timezone.datetime(2026, 1, 31, tzinfo=timezone.utc)
        entry = await ScheduledAnnouncement.objects.acreate(
            message="monthly",
            scheduled_at=jan_31,
            repeat=ScheduledAnnouncement.Repeat.MONTHLY,
        )
        feb = entry.occurrence_at(1)
        self.assertEqual((feb.year, feb.month, feb.day), (2026, 2, 28))
        mar = entry.occurrence_at(2)
        self.assertEqual((mar.year, mar.month, mar.day), (2026, 3, 31))


class CurrentPinnedMessageTestCase(TestCase):
    async def test_empty_when_nothing_live(self):
        self.assertEqual(await current_pinned_message(), "")

    async def test_returns_most_recent_live(self):
        await ScheduledAnnouncement.objects.acreate(
            message="older", scheduled_at=timezone.now() - timedelta(days=2)
        )
        await ScheduledAnnouncement.objects.acreate(
            message="newer", scheduled_at=timezone.now() - timedelta(hours=1)
        )
        self.assertEqual(await current_pinned_message(), "newer")

    async def test_ignores_future_and_disabled(self):
        await ScheduledAnnouncement.objects.acreate(
            message="future",
            scheduled_at=timezone.now() + timedelta(hours=1),
            enabled=True,
        )
        await ScheduledAnnouncement.objects.acreate(
            message="disabled",
            scheduled_at=timezone.now() - timedelta(hours=1),
            enabled=False,
        )
        self.assertEqual(await current_pinned_message(), "")


class DrivePinnedAnnouncementTestCase(TestCase):
    async def test_pushes_current_message_to_mod(self):
        await ScheduledAnnouncement.objects.acreate(
            message="hello from admin",
            scheduled_at=timezone.now() - timedelta(hours=1),
        )
        mod = AsyncMock()
        mod.post = AsyncMock()
        await drive_pinned_announcement({"http_client_mod": mod})
        mod.post.assert_called_once_with("/pin", json={"message": "hello from admin"})

    async def test_no_mod_session_skips(self):
        await ScheduledAnnouncement.objects.acreate(
            message="x", scheduled_at=timezone.now() - timedelta(hours=1)
        )
        # Should not raise even with no client.
        await drive_pinned_announcement({"http_client_mod": None})


class AnnounceServerRestartTestCase(TestCase):
    async def test_pushes_restart_message_to_mod(self):
        mod = AsyncMock()
        mod.post = AsyncMock()
        await announce_server_restart({"http_client_mod": mod})
        mod.post.assert_called_once_with("/pin", json={"message": RESTART_MESSAGE})

    async def test_swallows_mod_failure(self):
        mod = AsyncMock()
        mod.post = AsyncMock(side_effect=RuntimeError("mod unreachable"))
        # Should not raise even when the mod rejects the push.
        await announce_server_restart({"http_client_mod": mod})

    async def test_no_mod_session_skips(self):
        await announce_server_restart({"http_client_mod": None})
