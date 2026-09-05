"""Exclusive-progression flag (Character.exclusive_progression).

Design (freeman, 2026-09-01): Motor Town progression is client-side, so the
login burst of PlayerLevelChanged lines (the game logs all 7 level types right
after every login) carries the player's login-time levels. Mid-session events
keep the stored Character levels current, so at each login any burst value
ABOVE what the player's own observed sessions left behind means they leveled
outside this server. A character whose entire level table is all-1 is a fresh
account and gets armed (flag=True); an armed character whose login snapshot
exceeds the stored levels gets broken (flag=False).
"""

from django.test import TestCase
from django.utils import timezone

import amc.tasks as tasks_module
from amc.models import Character, Player
from amc.server_logs import (
    PlayerLevelChangedLogEvent,
    PlayerLoginLogEvent,
)
from amc.tasks import process_log_event

ALL_LEVEL_TYPES = [
    "CL_Driver",
    "CL_Taxi",
    "CL_Bus",
    "CL_Truck",
    "CL_Racer",
    "CL_Wrecker",
    "CL_Police",
]


class ExclusiveProgressionTests(TestCase):
    def setUp(self):
        # The login-window tracker is module-level; keep tests isolated.
        tasks_module._login_level_types_seen.clear()
        tasks_module._logins_since_restart.clear()
        tasks_module._unjudged_bursts.clear()

    async def _login(self, player_id: int, player_name: str):
        # Mark this player as having logged in since the last (worker) start,
        # so the burst is judged — the ServerStarted-forgiveness behavior
        # (first login unjudged) is covered by
        # test_exclusive_progression_fixes.py.
        tasks_module._logins_since_restart.add(player_id)
        await process_log_event(
            PlayerLoginLogEvent(
                timestamp=timezone.now(),
                player_name=player_name,
                player_id=player_id,
            ),
            ctx={},
        )

    async def _level(
        self, player_id: int, player_name: str, level_type: str, value: int
    ):
        await process_log_event(
            PlayerLevelChangedLogEvent(
                timestamp=timezone.now(),
                player_name=player_name,
                player_id=player_id,
                level_type=level_type,
                level_value=value,
            ),
            ctx={},
        )

    async def _burst(self, player_id: int, player_name: str, levels: dict):
        """Send a full 7-type login burst (the game does this on every login)."""
        for level_type in ALL_LEVEL_TYPES:
            await self._level(
                player_id, player_name, level_type, levels.get(level_type, 1)
            )

    async def _armed_character(self, unique_id: int, name: str, **levels):
        player = await Player.objects.acreate(unique_id=unique_id)
        await Character.objects.acreate(
            name=name,
            player=player,
            guid=f"guid_{unique_id}",
            exclusive_progression=True,
            **levels,
        )
        return await Character.objects.aget(name=name)

    async def _untracked_character(self, unique_id: int, name: str):
        """Character row as the login handler would have created it (no levels)."""
        player = await Player.objects.acreate(unique_id=unique_id)
        await Character.objects.acreate(
            name=name,
            player=player,
            guid=f"guid_{unique_id}",
            exclusive_progression=None,
        )
        return await Character.objects.aget(name=name)

    async def test_new_player_all_one_arms_flag(self):
        """A genuinely fresh account (entire level table all-1) is armed."""
        await self._untracked_character(555101, "FreshDriver")
        await self._login(555101, "FreshDriver")
        await self._burst(555101, "FreshDriver", {})

        character = await Character.objects.aget(name="FreshDriver")
        self.assertIs(character.exclusive_progression, True)
        self.assertEqual(character.driver_level, 1)
        self.assertEqual(character.truck_level, 1)

    async def test_veteran_first_burst_not_armed(self):
        """A character whose client save already has levels is not tracked."""
        await self._untracked_character(555102, "OldVeteran")
        await self._login(555102, "OldVeteran")
        await self._burst(
            555102,
            "OldVeteran",
            {"CL_Driver": 50, "CL_Truck": 30, "CL_Taxi": 10},
        )

        character = await Character.objects.aget(name="OldVeteran")
        self.assertIsNone(character.exclusive_progression)
        self.assertEqual(character.driver_level, 50)

    async def test_armed_login_unchanged_stays_true(self):
        """Returning with exactly the stored levels keeps the flag True."""
        await self._armed_character(
            555103,
            "LoyalDriver",
            driver_level=3,
            truck_level=2,
        )
        await self._login(555103, "LoyalDriver")
        await self._burst(
            555103,
            "LoyalDriver",
            {"CL_Driver": 3, "CL_Truck": 2},
        )

        character = await Character.objects.aget(name="LoyalDriver")
        self.assertIs(character.exclusive_progression, True)

    async def test_armed_login_increase_breaks_flag(self):
        """Login snapshot above the stored levels = progressed outside."""
        await self._armed_character(
            555104,
            "Hopper",
            driver_level=3,
        )
        await self._login(555104, "Hopper")
        await self._burst(
            555104,
            "Hopper",
            {"CL_Driver": 5},
        )

        character = await Character.objects.aget(name="Hopper")
        self.assertIs(character.exclusive_progression, False)
        # The stored level is still synced to the client's current value.
        self.assertEqual(character.driver_level, 5)

    async def test_midsession_gain_keeps_flag_true(self):
        """In-session gains (natural or exp grants) never touch the flag."""
        await self._armed_character(
            555105,
            "Grinder",
            driver_level=3,
        )
        # No login event: these are mid-session events.
        await self._level(555105, "Grinder", "CL_Driver", 4)
        await self._level(555105, "Grinder", "CL_Driver", 9)  # exp grant jump

        character = await Character.objects.aget(name="Grinder")
        self.assertIs(character.exclusive_progression, True)
        self.assertEqual(character.driver_level, 9)

    async def test_regression_at_login_updates_but_keeps_flag(self):
        """A rolled-back client save is an anomaly but not 'leveled outside'."""
        await self._armed_character(
            555106,
            "Reverter",
            driver_level=5,
        )
        await self._login(555106, "Reverter")
        await self._burst(
            555106,
            "Reverter",
            {"CL_Driver": 4},
        )

        character = await Character.objects.aget(name="Reverter")
        self.assertIs(character.exclusive_progression, True)
        self.assertEqual(character.driver_level, 4)

    async def test_arming_completes_after_interrupted_first_burst(self):
        """A first burst cut short by a worker restart arms on the next login."""
        await self._untracked_character(555107, "LateArmer")
        await self._login(555107, "LateArmer")
        await self._level(555107, "LateArmer", "CL_Driver", 1)
        await self._level(555107, "LateArmer", "CL_Taxi", 1)

        character = await Character.objects.aget(name="LateArmer")
        self.assertIsNone(character.exclusive_progression)

        # Simulate a worker restart losing the in-memory login-window state.
        tasks_module._login_level_types_seen.clear()
        await self._login(555107, "LateArmer")
        for level_type in ("CL_Bus", "CL_Truck", "CL_Racer", "CL_Wrecker", "CL_Police"):
            await self._level(555107, "LateArmer", level_type, 1)

        character = await Character.objects.aget(name="LateArmer")
        self.assertIs(character.exclusive_progression, True)

    async def test_tagged_name_event_updates_same_character(self):
        """Tagged display names ('[R] Name') must still land on the same row."""
        await self._armed_character(
            555108,
            "Tagged",
            driver_level=3,
        )
        # Mid-session event carrying the tagged display name.
        await self._level(555108, "[R] Tagged", "CL_Driver", 4)

        characters = [c async for c in Character.objects.all()]
        self.assertEqual(len(characters), 1)
        self.assertEqual(characters[0].name, "Tagged")
        self.assertEqual(characters[0].driver_level, 4)
        self.assertIs(characters[0].exclusive_progression, True)

    async def test_eighth_event_after_full_burst_is_midsession(self):
        """Once the 7-type burst is processed, later events are in-session."""
        await self._armed_character(
            555109,
            "SessionDriver",
            driver_level=3,
        )
        await self._login(555109, "SessionDriver")
        await self._burst(
            555109,
            "SessionDriver",
            {"CL_Driver": 3},
        )
        # In-session natural gain after the burst completed.
        await self._level(555109, "SessionDriver", "CL_Driver", 4)

        character = await Character.objects.aget(name="SessionDriver")
        self.assertIs(character.exclusive_progression, True)
        self.assertEqual(character.driver_level, 4)
