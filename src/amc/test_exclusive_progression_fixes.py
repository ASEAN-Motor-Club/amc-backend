"""Regression tests for the PR #66 review fixes (2026-09-05).

C1  processing-order inversion — per-player asyncio lock serializes login +
    level events so burst classification keys off log order, not completion.
C2  tagged-name fallback — NULL last_login must sort LAST (Postgres DESC
    default put a never-logged-in character first).
H1  ServerStarted forgiveness — first login of each player after a restart is
    refresh-only; breaks are persisted to ExclusiveProgressionBreak.
H2  exact-name match — prefer GUID rows, then newest, on transient
    GUID-less duplicates.
M1  regression warning gated to tracked characters.
"""

import asyncio
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

import pytest

import amc.tasks as tasks_module
from amc.command_framework import CommandContext
from amc.commands.admin import cmd_exclusive_unbreak
from amc.models import Character, ExclusiveProgressionBreak, Player, PlayerStatusLog
from amc.server_logs import (
    PlayerLevelChangedLogEvent,
    PlayerLoginLogEvent,
    ServerStartedLogEvent,
)
from amc.tasks import process_log_event

ALL_LEVEL_TYPES = [
    "CL_Driver", "CL_Taxi", "CL_Bus", "CL_Truck",
    "CL_Racer", "CL_Wrecker", "CL_Police",
]


class C2FallbackOrderingTests(TestCase):
    """The tagged-name fallback must not land on a never-logged-in character."""

    def setUp(self):
        tasks_module._login_level_types_seen.clear()
        tasks_module._logins_since_restart.clear()
        tasks_module._unjudged_bursts.clear()

    async def test_active_character_wins_over_null_last_login(self):
        player = await Player.objects.acreate(unique_id=777001)
        # A: established character, logged in (has a status log → last_login set)
        char_a = await Character.objects.acreate(
            name="Active", player=player, guid="guid_a777",
            exclusive_progression=True, driver_level=3,
        )
        await PlayerStatusLog.objects.acreate(
            character=char_a,
            timespan=(timezone.now() - timezone.timedelta(hours=1), None),
        )
        # B: brand-new character, never logged in (no status logs → NULL)
        char_b = await Character.objects.acreate(
            name="Newbie", player=player, guid="guid_b777",
            exclusive_progression=True, driver_level=1,
        )

        # Not the first login since (worker) start → the burst is judged.
        tasks_module._logins_since_restart.add(777001)
        await process_log_event(
            PlayerLoginLogEvent(
                timestamp=timezone.now(), player_name="Active", player_id=777001
            ),
            ctx={},
        )
        # Tagged display name — exact match fails, fallback must pick A.
        await process_log_event(
            PlayerLevelChangedLogEvent(
                timestamp=timezone.now(), player_name="[R] Active",
                player_id=777001, level_type="CL_Driver", level_value=4,
            ),
            ctx={},
        )
        await char_a.arefresh_from_db()
        await char_b.arefresh_from_db()
        # The event landed on A (the real target), not on NULL-last_login B.
        self.assertEqual(char_a.driver_level, 4)
        self.assertEqual(char_b.driver_level, 1)
        self.assertIs(char_a.exclusive_progression, False)  # judged break: 3→4
        self.assertIs(char_b.exclusive_progression, True)   # untouched


class C1OrderingInversionTests(TestCase):
    """In-session gain processed before its burst line must not break the flag."""

    def setUp(self):
        tasks_module._login_level_types_seen.clear()
        tasks_module._logins_since_restart.clear()
        tasks_module._unjudged_bursts.clear()

    async def test_gain_before_burst_classifies_in_session(self):
        player = await Player.objects.acreate(unique_id=777002)
        char = await Character.objects.acreate(
            name="Racer", player=player, guid="guid_c777",
            exclusive_progression=True, driver_level=3,
        )
        ts = timezone.now()
        # Real pipeline: lines are enqueued in log order (login → burst(3) →
        # gain(4)) but arq's max_jobs lets them run concurrently.  The
        # per-player lock must impose enqueue order on classification.
        login = PlayerLoginLogEvent(timestamp=ts, player_name="Racer", player_id=777002)
        burst = PlayerLevelChangedLogEvent(
            timestamp=ts + timezone.timedelta(seconds=1),
            player_name="Racer", player_id=777002,
            level_type="CL_Driver", level_value=3,
        )
        gain = PlayerLevelChangedLogEvent(
            timestamp=ts + timezone.timedelta(seconds=30),
            player_name="Racer", player_id=777002,
            level_type="CL_Driver", level_value=4,  # genuine in-session gain
        )
        # Not the first login since (worker) start → the burst is judged, so
        # the test actually exercises classification order.
        tasks_module._logins_since_restart.add(777002)
        await asyncio.gather(
            process_log_event(login, ctx={}),
            process_log_event(burst, ctx={}),
            process_log_event(gain, ctx={}),
        )

        await char.arefresh_from_db()
        # burst(3) classified as the login snapshot (3 == stored 3 → no
        # break), gain(4) classified in-session.  Without the lock the gain
        # can classify first: 4 > 3 → false break.
        self.assertIs(char.exclusive_progression, True)
        self.assertEqual(char.driver_level, 4)  # last log-order value wins

    async def test_lock_serializes_same_player_events(self):
        # The lock must exist per player and be reusable across events.
        lock = tasks_module._player_level_lock(42)
        self.assertIs(lock, tasks_module._player_level_lock(42))
        self.assertIsNot(lock, tasks_module._player_level_lock(43))


class ServerStartedForgivenessTests(TestCase):
    """First login after a restart is refresh-only — no break judgment."""

    def setUp(self):
        tasks_module._login_level_types_seen.clear()
        tasks_module._logins_since_restart.clear()
        tasks_module._unjudged_bursts.clear()

    async def _login(self, player_id, player_name):
        await process_log_event(
            PlayerLoginLogEvent(
                timestamp=timezone.now(), player_name=player_name, player_id=player_id
            ),
            ctx={},
        )

    async def _level(self, player_id, player_name, level_type, value):
        await process_log_event(
            PlayerLevelChangedLogEvent(
                timestamp=timezone.now(), player_name=player_name,
                player_id=player_id, level_type=level_type, level_value=value,
            ),
            ctx={},
        )

    async def test_first_login_after_restart_is_unjudged(self):
        player = await Player.objects.acreate(unique_id=777003)
        char = await Character.objects.acreate(
            name="PostRestart", player=player, guid="guid_d777",
            exclusive_progression=True, driver_level=3,
        )
        # Server restart clears state.
        await process_log_event(
            ServerStartedLogEvent(timestamp=timezone.now(), version="1.0"),
            ctx={},
        )
        await self._login(777003, "PostRestart")
        # Crash-eaten gains scenario: login snapshot shows 4 > stored 3, but
        # this is the first login after a restart → refresh-only, no break.
        await self._level(777003, "PostRestart", "CL_Driver", 4)

        await char.arefresh_from_db()
        self.assertIs(char.exclusive_progression, True)  # forgiven
        self.assertEqual(char.driver_level, 4)           # refreshed
        self.assertEqual(
            await ExclusiveProgressionBreak.objects.acount(), 0
        )

    async def test_second_login_after_restart_judges(self):
        player = await Player.objects.acreate(unique_id=777004)
        char = await Character.objects.acreate(
            name="SecondLogin", player=player, guid="guid_e777",
            exclusive_progression=True, driver_level=3,
        )
        await process_log_event(
            ServerStartedLogEvent(timestamp=timezone.now(), version="1.0"),
            ctx={},
        )
        # First login: forgiven (snapshot refreshes stored to 4).
        await self._login(777004, "SecondLogin")
        await self._level(777004, "SecondLogin", "CL_Driver", 4)
        # Second login: judged normally — but the first login is unjudged only
        # for ITS burst window; a second login must judge even though the
        # first burst never completed.
        await self._login(777004, "SecondLogin")
        await self._level(777004, "SecondLogin", "CL_Driver", 5)

        await char.arefresh_from_db()
        self.assertIs(char.exclusive_progression, False)
        self.assertEqual(await ExclusiveProgressionBreak.objects.acount(), 1)

    async def test_break_is_persisted_with_old_and_new(self):
        player = await Player.objects.acreate(unique_id=777005)
        char = await Character.objects.acreate(
            name="Audited", player=player, guid="guid_f777",
            exclusive_progression=True, truck_level=1,
        )
        # Not the first login since (worker) start → burst is judged.
        tasks_module._logins_since_restart.add(777005)
        await self._login(777005, "Audited")
        await self._level(777005, "Audited", "CL_Truck", 9)

        break_row = await ExclusiveProgressionBreak.objects.afirst()
        self.assertIsNotNone(break_row)
        self.assertEqual(break_row.character_id, char.pk)
        self.assertEqual(break_row.level_field, "truck_level")
        self.assertEqual(break_row.stored_level, 1)
        self.assertEqual(break_row.seen_level, 9)


class H2ExactMatchOrderingTests(TestCase):
    """Exact-name match prefers GUID rows, then newest (transient dupes)."""

    def setUp(self):
        tasks_module._login_level_types_seen.clear()
        tasks_module._logins_since_restart.clear()
        tasks_module._unjudged_bursts.clear()

    async def test_guid_row_preferred_over_older_guidless_dupe(self):
        player = await Player.objects.acreate(unique_id=777006)
        # Older GUID-less transient duplicate.
        await Character.objects.acreate(
            name="Dup", player=player, guid=None, driver_level=0,
        )
        # Newer GUID row — must win.
        char = await Character.objects.acreate(
            name="Dup", player=player, guid="guid_g777", driver_level=2,
        )
        tasks_module._logins_since_restart.add(777006)
        await process_log_event(
            PlayerLoginLogEvent(timestamp=timezone.now(), player_name="Dup", player_id=777006),
            ctx={},
        )
        await process_log_event(
            PlayerLevelChangedLogEvent(
                timestamp=timezone.now(), player_name="Dup",
                player_id=777006, level_type="CL_Driver", level_value=3,
            ),
            ctx={},
        )
        await char.arefresh_from_db()
        self.assertEqual(char.driver_level, 3)


class M1RegressionLogTests(TestCase):
    """Regression-at-login warning only fires for tracked characters."""

    def setUp(self):
        tasks_module._login_level_types_seen.clear()
        tasks_module._logins_since_restart.clear()
        tasks_module._unjudged_bursts.clear()

    async def test_untracked_rollback_does_not_warn_or_break(self):
        player = await Player.objects.acreate(unique_id=777007)
        char = await Character.objects.acreate(
            name="OldVeteran", player=player, guid="guid_h777",
            exclusive_progression=None, driver_level=50,
        )
        tasks_module._logins_since_restart.add(777007)
        await process_log_event(
            PlayerLoginLogEvent(timestamp=timezone.now(), player_name="OldVeteran", player_id=777007),
            ctx={},
        )
        await process_log_event(
            PlayerLevelChangedLogEvent(
                timestamp=timezone.now(), player_name="OldVeteran",
                player_id=777007, level_type="CL_Driver", level_value=49,  # rollback
            ),
            ctx={},
        )
        await char.arefresh_from_db()
        self.assertIsNone(char.exclusive_progression)  # stays untracked
        self.assertEqual(char.driver_level, 49)        # still kept current


def _cmd_ctx(player, character, player_info):
    return CommandContext(
        timestamp=None,
        character=character,
        player=player,
        http_client=MagicMock(),
        http_client_mod=MagicMock(),
        player_info=player_info,
    )


class ExclusiveUnbreakCommandTests(TestCase):
    def setUp(self):
        tasks_module._login_level_types_seen.clear()
        tasks_module._logins_since_restart.clear()
        tasks_module._unjudged_bursts.clear()

    async def _pair(self, char_name="BrokenRunner", **char_kwargs):
        player = await Player.objects.acreate(unique_id=777100)
        char = await Character.objects.acreate(
            name=char_name, player=player, guid="guid_unbreak", **char_kwargs
        )
        return player, char

    @pytest.mark.asyncio
    async def test_non_admin_is_noop(self):
        player, char = await self._pair(exclusive_progression=False)
        ctx = _cmd_ctx(player, char, {"bIsAdmin": False})
        with patch("amc.mod_server.show_popup"):
            await cmd_exclusive_unbreak(ctx, "BrokenRunner")
        await char.arefresh_from_db()
        self.assertIs(char.exclusive_progression, False)

    @pytest.mark.asyncio
    async def test_rearms_broken_flag(self):
        player, char = await self._pair(exclusive_progression=False)
        await ExclusiveProgressionBreak.objects.acreate(
            character=char, level_field="driver_level",
            stored_level=3, seen_level=7,
        )
        ctx = _cmd_ctx(player, char, {"bIsAdmin": True})
        with patch("amc.mod_server.show_popup"):
            await cmd_exclusive_unbreak(ctx, "BrokenRunner")
        await char.arefresh_from_db()
        self.assertIs(char.exclusive_progression, True)

    @pytest.mark.asyncio
    async def test_already_armed_is_noop_reply(self):
        player, char = await self._pair(exclusive_progression=True)
        ctx = _cmd_ctx(player, char, {"bIsAdmin": True})
        with patch("amc.mod_server.show_popup"):
            await cmd_exclusive_unbreak(ctx, "BrokenRunner")
        await char.arefresh_from_db()
        self.assertIs(char.exclusive_progression, True)

    @pytest.mark.asyncio
    async def test_unknown_name_is_noop(self):
        player, char = await self._pair(exclusive_progression=False)
        ctx = _cmd_ctx(player, char, {"bIsAdmin": True})
        with patch("amc.mod_server.show_popup"):
            await cmd_exclusive_unbreak(ctx, "Nobody")
        await char.arefresh_from_db()
        self.assertIs(char.exclusive_progression, False)


class FreshCharacterArmingTests(TestCase):
    """A brand-new level-1 character gets the flag during its FIRST login.

    Real sequence (freeman 2026-09-05): the login line creates the Character
    row (level fields NULL, flag NULL via aupdate_or_create defaults), then
    the client uploads its table as the 7-line all-1 burst ~2s later.  The
    arming check (whole table == 1) passes exactly at the last burst event —
    the flag is armed on first login, not one login late.  Arming is also not
    gated on the ServerStarted-forgiveness `judged` flag: a brand-new
    player's first login post-restart still arms.
    """

    def setUp(self):
        tasks_module._login_level_types_seen.clear()
        tasks_module._logins_since_restart.clear()
        tasks_module._unjudged_bursts.clear()

    async def test_arms_at_first_login_burst_completion(self):
        # Row exactly as the login handler creates it: everything NULL.
        player = await Player.objects.acreate(unique_id=777200)
        char = await Character.objects.acreate(
            name="BrandNew", player=player, guid="guid_new777",
        )
        self.assertIsNone(char.exclusive_progression)
        self.assertIsNone(char.driver_level)

        # First login since worker start (no ServerStarted seen, marker set
        # empty) → this burst is also the forgiveness-unjudged window; arming
        # must fire anyway.
        await process_log_event(
            PlayerLoginLogEvent(
                timestamp=timezone.now(), player_name="BrandNew", player_id=777200
            ),
            ctx={},
        )

        # The client uploads its table: all 7 types at level 1.
        for i, level_type in enumerate(ALL_LEVEL_TYPES):
            await process_log_event(
                PlayerLevelChangedLogEvent(
                    timestamp=timezone.now(), player_name="BrandNew",
                    player_id=777200, level_type=level_type, level_value=1,
                ),
                ctx={},
            )
            await char.arefresh_from_db()
            if i < len(ALL_LEVEL_TYPES) - 1:
                # Not armed before the whole table is observable.
                self.assertIsNone(
                    char.exclusive_progression,
                    f"premature arm after {level_type}",
                )
        self.assertIs(char.exclusive_progression, True)

        # Second login (judged): burst of 1s == stored → flag survives.
        await process_log_event(
            PlayerLoginLogEvent(
                timestamp=timezone.now(), player_name="BrandNew", player_id=777200
            ),
            ctx={},
        )
        for level_type in ALL_LEVEL_TYPES:
            await process_log_event(
                PlayerLevelChangedLogEvent(
                    timestamp=timezone.now(), player_name="BrandNew",
                    player_id=777200, level_type=level_type, level_value=1,
                ),
                ctx={},
            )
        await char.arefresh_from_db()
        self.assertIs(char.exclusive_progression, True)

    async def test_mid_burst_row_creation_arms_second_login(self):
        """Documented M2 residual: when the mod API is slow and the row only
        comes into existence mid-burst, the skipped early types leave NULL
        stored levels, so the all-1 arming check can't pass until the second
        login's burst."""
        player = await Player.objects.acreate(unique_id=777201)
        tasks_module._logins_since_restart.add(777201)  # judged burst
        # Login could not create the row (GUID unresolved) — the first two
        # burst lines are skipped (no row yet).
        await process_log_event(
            PlayerLoginLogEvent(
                timestamp=timezone.now(), player_name="LateRow", player_id=777201
            ),
            ctx={},
        )
        for level_type in ALL_LEVEL_TYPES[:2]:
            await process_log_event(
                PlayerLevelChangedLogEvent(
                    timestamp=timezone.now(), player_name="LateRow",
                    player_id=777201, level_type=level_type, level_value=1,
                ),
                ctx={},
            )
        # Row appears (created by a later pipeline event, e.g. the mod roster
        # sync) — remaining burst lines land on it.
        char = await Character.objects.acreate(
            name="LateRow", player=player, guid="guid_late777",
        )
        for level_type in ALL_LEVEL_TYPES[2:]:
            await process_log_event(
                PlayerLevelChangedLogEvent(
                    timestamp=timezone.now(), player_name="LateRow",
                    player_id=777201, level_type=level_type, level_value=1,
                ),
                ctx={},
            )
        await char.arefresh_from_db()
        # driver/taxi were skipped while the row was missing → NULL → the
        # first login cannot arm.
        self.assertIsNone(char.driver_level)
        self.assertIsNone(char.exclusive_progression)

        # Second login: full burst observed → armed.
        await process_log_event(
            PlayerLoginLogEvent(
                timestamp=timezone.now(), player_name="LateRow", player_id=777201
            ),
            ctx={},
        )
        for level_type in ALL_LEVEL_TYPES:
            await process_log_event(
                PlayerLevelChangedLogEvent(
                    timestamp=timezone.now(), player_name="LateRow",
                    player_id=777201, level_type=level_type, level_value=1,
                ),
                ctx={},
            )
        await char.arefresh_from_db()
        self.assertIs(char.exclusive_progression, True)
        self.assertEqual(char.driver_level, 1)
