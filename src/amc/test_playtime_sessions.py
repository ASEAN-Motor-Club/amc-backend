"""Session-integrity tests for play-time tracking.

Regression coverage for the bug where ``/player_profile`` reported thousands of
hours: a lost logout left a range open forever, and the next logout that did
arrive stretched an older range across weeks.

Run with pytest (``pytest src/amc/test_playtime_sessions.py``). The handlers
are coroutines, so each test drives them with ``async_to_sync`` inside a normal
``TestCase`` — a bare ``async def test_*`` method is silently NOT executed by
this repo's pytest config (no ``asyncio_mode = auto``).
"""

from datetime import timedelta

from asgiref.sync import async_to_sync
from django.test import TestCase
from django.utils import timezone
from psycopg.types.range import Range

from amc import sessions as integrity
from amc.models import (
    Character,
    Player,
    PlayerVehicleLog,
    PlayerStatusLog,
    ServerLog,
)
from amc.sessions import SessionEvent, build_session_ranges
from amc.tasks import process_login_event, process_logout_event

T0 = timezone.now() - timedelta(days=30)


def make_range(start, end):
    return Range(start, end, bounds="[)")


class _SessionIntegrityBase(TestCase):
    def setUp(self):
        self.player = Player.objects.create(unique_id=76561190000000001)
        self.character = Character.objects.create(
            name="Goji", player=self.player, guid="goji-guid"
        )

    def open_session(self, start):
        return PlayerStatusLog.objects.create(
            character=self.character, timespan=make_range(start, None), host="h1"
        )

    def closed_session(self, start, end):
        return PlayerStatusLog.objects.create(
            character=self.character, timespan=make_range(start, end), host="h1"
        )

    def log_line(self, timestamp, text, hostname="asean-mt-server"):
        return ServerLog.objects.create(
            timestamp=timestamp, text=text, hostname=hostname
        )

    def rows(self):
        return list(
            PlayerStatusLog.objects.filter(character=self.character).order_by(
                "timespan"
            )
        )


class TestLoginEvents(_SessionIntegrityBase):
    def test_login_closes_the_previous_session(self):
        start = T0
        self.open_session(start)
        login_at = start + timedelta(hours=2)

        async_to_sync(process_login_event)(self.character.id, login_at, "h1")

        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].timespan.lower, start)
        self.assertEqual(rows[0].timespan.upper, login_at)
        self.assertIsNone(rows[1].timespan.upper)
        self.assertEqual(rows[1].timespan.lower, login_at)
        self.assertEqual(rows[1].host, "h1")

    def test_login_does_not_stretch_a_dormant_session(self):
        """A missing logout must not turn a month-old range into a month-long one."""
        start = T0
        self.open_session(start)
        # The logout line was lost to the handler, but the log itself has it.
        logout_seen = start + timedelta(hours=3)
        self.log_line(
            logout_seen,
            f"Player Logout: [M] Goji ({self.player.unique_id})",
        )
        login_at = timezone.now()

        async_to_sync(process_login_event)(self.character.id, login_at, "h1")

        closed = PlayerStatusLog.objects.get(
            character=self.character, timespan__upper_inf=False
        )
        self.assertEqual(closed.timespan.upper, logout_seen)

    def test_login_leaves_dormant_session_open_without_evidence(self):
        start = T0
        self.open_session(start)
        login_at = timezone.now()

        async_to_sync(process_login_event)(self.character.id, login_at, "h1")

        open_rows = PlayerStatusLog.objects.filter(
            character=self.character, timespan__upper_inf=True
        )
        # Both stay open: the old one cannot be closed without evidence, and
        # the new login is a separate session.
        self.assertEqual(open_rows.count(), 2)

    def test_duplicate_login_line_is_idempotent(self):
        login_at = timezone.now()
        async_to_sync(process_login_event)(self.character.id, login_at, "h1")
        async_to_sync(process_login_event)(self.character.id, login_at, "h1")

        self.assertEqual(PlayerStatusLog.objects.count(), 1)


class TestLogoutEvents(_SessionIntegrityBase):
    def test_logout_closes_the_open_session(self):
        start = T0
        self.open_session(start)
        logout_at = start + timedelta(hours=4)

        async_to_sync(process_logout_event)(self.character.id, logout_at)

        row = PlayerStatusLog.objects.get(character=self.character)
        self.assertEqual(row.timespan.upper, logout_at)

    def test_logout_without_open_session_writes_nothing(self):
        """The old code inserted a (-infinity, ts) range here."""
        async_to_sync(process_logout_event)(self.character.id, timezone.now())

        self.assertEqual(PlayerStatusLog.objects.count(), 0)

    def test_logout_does_not_close_a_dormant_session_at_its_own_timestamp(self):
        start = T0
        self.open_session(start)
        activity = start + timedelta(hours=5)
        PlayerVehicleLog.objects.create(
            character=self.character,
            timestamp=activity,
            action=PlayerVehicleLog.Action.ENTERED,
            vehicle_name="Zino",
        )
        logout_at = timezone.now()

        async_to_sync(process_logout_event)(self.character.id, logout_at)

        row = PlayerStatusLog.objects.get(character=self.character)
        self.assertEqual(row.timespan.upper, activity)


class TestRestarts(_SessionIntegrityBase):
    def test_restart_closes_only_its_own_host(self):
        other = Character.objects.create(
            name="Kaizu", player=self.player, guid="kaizu-guid"
        )
        started = timezone.now() - timedelta(hours=3)
        PlayerStatusLog.objects.create(
            character=self.character, timespan=make_range(started, None), host="main"
        )
        PlayerStatusLog.objects.create(
            character=other, timespan=make_range(started, None), host="event"
        )

        async_to_sync(integrity.close_sessions_on_restart)("main", timezone.now())

        self.assertEqual(
            PlayerStatusLog.objects.get(character=self.character).timespan.upper
            is not None,
            True,
        )
        self.assertTrue(PlayerStatusLog.objects.get(character=other).timespan.upper_inf)

    def test_restart_leaves_stale_sessions_untouched(self):
        self.open_session(T0)

        async_to_sync(integrity.close_sessions_on_restart)("h1", timezone.now())

        self.assertTrue(PlayerStatusLog.objects.get().timespan.upper_inf)

    def test_restart_ignores_legacy_rows_without_host(self):
        PlayerStatusLog.objects.create(
            character=self.character,
            timespan=make_range(timezone.now() - timedelta(hours=2), None),
            host="",
        )

        async_to_sync(integrity.close_sessions_on_restart)("h1", timezone.now())

        self.assertTrue(PlayerStatusLog.objects.get().timespan.upper_inf)


class TestRestartAsEvidence(_SessionIntegrityBase):
    """A restart disconnects everyone on that host, so it bounds the session."""

    def restart_line(self, timestamp, hostname):
        self.log_line(
            timestamp, "DedicatedServer is started. version: 0.7.13", hostname
        )

    def test_login_closes_at_the_restart_when_the_logout_was_lost(self):
        start = T0
        self.open_session(start)  # host="h1"
        restart_at = start + timedelta(hours=4)
        self.restart_line(restart_at, "h1")
        login_at = timezone.now()  # 30 days later, nothing else logged

        async_to_sync(process_login_event)(self.character.id, login_at, "h1")

        closed = PlayerStatusLog.objects.get(
            character=self.character, timespan__upper_inf=False
        )
        self.assertEqual(closed.timespan.upper, restart_at)

    def test_restart_of_another_host_is_not_evidence(self):
        start = T0
        self.open_session(start)  # host="h1"
        self.restart_line(start + timedelta(hours=4), "h2")
        login_at = timezone.now()

        async_to_sync(process_login_event)(self.character.id, login_at, "h1")

        # No proof for an h1 session ended: it must stay open, not be stretched.
        self.assertEqual(
            PlayerStatusLog.objects.filter(timespan__upper_inf=True).count(), 2
        )

    def test_restart_bounds_rebuilt_ranges(self):
        from django.core.management import call_command

        start = T0
        restart_at = start + timedelta(hours=5)
        self.log_line(start, f"Player Login: Goji ({self.player.unique_id})")
        self.restart_line(restart_at, "asean-mt-server")
        self.log_line(
            start + timedelta(hours=9),
            f"Player Logout: Goji ({self.player.unique_id})",
        )

        call_command("repair_player_sessions", "--apply", "--character", "Goji")

        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].timespan.upper, restart_at)


class TestBuildSessionRanges(TestCase):
    def test_login_logout_pair(self):
        events = [
            SessionEvent(T0, "login", 1, "h1"),
            SessionEvent(T0 + timedelta(hours=2), "logout", 1, "h1"),
        ]
        self.assertEqual(
            build_session_ranges(events),
            [(1, T0, T0 + timedelta(hours=2), "h1")],
        )

    def test_login_superseded_by_next_login(self):
        events = [
            SessionEvent(T0, "login", 1, "h1"),
            SessionEvent(T0 + timedelta(hours=1), "login", 1, "h1"),
        ]
        ranges = build_session_ranges(events)
        self.assertEqual(len(ranges), 2)
        self.assertEqual(ranges[0][2], T0 + timedelta(hours=1))
        self.assertIsNone(ranges[1][2])

    def test_restart_splits_only_matching_host(self):
        events = [
            SessionEvent(T0, "login", 1, "main"),
            SessionEvent(T0, "login", 2, "event"),
            SessionEvent(T0 + timedelta(hours=1), "restart", None, "main"),
        ]
        ranges = {
            cid: (start, end) for cid, start, end, _h in build_session_ranges(events)
        }
        self.assertEqual(ranges[1][1], T0 + timedelta(hours=1))
        self.assertIsNone(ranges[2][1])

    def test_out_of_order_events_are_sorted(self):
        events = [
            SessionEvent(T0 + timedelta(hours=2), "logout", 1, "h1"),
            SessionEvent(T0, "login", 1, "h1"),
        ]
        self.assertEqual(len(build_session_ranges(events)), 1)


class TestRepairCommand(_SessionIntegrityBase):
    def test_rebuild_replaces_corrupt_rows(self):
        from django.core.management import call_command

        # Existing corruption: a 118-day session.
        corrupt_start = T0
        self.closed_session(corrupt_start, corrupt_start + timedelta(days=118))

        # Ground truth in the log.
        real_start = T0 + timedelta(days=1)
        real_end = real_start + timedelta(hours=3)
        self.log_line(real_start, f"Player Login: Goji ({self.player.unique_id})")
        self.log_line(real_end, f"Player Logout: Goji ({self.player.unique_id})")

        call_command("repair_player_sessions", "--apply", "--character", "Goji")

        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].timespan.lower, real_start)
        self.assertEqual(rows[0].timespan.upper, real_end)
        self.assertEqual(rows[0].host, "asean-mt-server")

    def test_dry_run_writes_nothing(self):
        from django.core.management import call_command

        self.closed_session(T0, T0 + timedelta(days=40))
        self.log_line(
            T0 + timedelta(days=1), f"Player Login: Goji ({self.player.unique_id})"
        )

        call_command("repair_player_sessions")

        self.assertEqual(PlayerStatusLog.objects.count(), 1)
        self.assertEqual(PlayerStatusLog.objects.get().duration, timedelta(days=40))


class TestProfileTotals(_SessionIntegrityBase):
    def test_implausible_sessions_are_not_counted(self):
        from unittest.mock import MagicMock

        from amc_cogs.profile import PlayerProfileCog

        self.closed_session(T0, T0 + timedelta(days=118))
        self.closed_session(T0, T0 + timedelta(hours=5))

        cog = PlayerProfileCog(MagicMock())
        embed = async_to_sync(cog._build_profile_embed)(self.character, self.player)
        activity = next(f for f in embed.fields if f.name == "🕒 Activity")
        self.assertIn("5.0h", activity.value)
        self.assertNotIn("2,832", activity.value)
