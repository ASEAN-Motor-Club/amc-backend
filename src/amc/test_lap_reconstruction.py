"""Lap reconstruction from the ServerPassedRaceSection SSE stream.

The SSE migration dropped mid-race BestLapTime/LapTimes sync (the old
polling cron was their only source). The section stream carries everything
needed: a section-0 crossing AFTER the first (start-line) crossing completes
a lap, and LaptimeSeconds is then the lap's time. Verified live 2026-09-05
(staging, yuyou 4-lap run): S0 splits 19.88/16.83 matched TotalTime deltas.
"""

import time
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase

from amc.factories import CharacterFactory, PlayerFactory
from amc.handlers import dispatch
from amc.handlers.events import _upsert_game_event, _upsert_game_event_character
from amc.models import GameEventCharacter
from amc.test_event_handlers import (
    CHAR_GUID,
    EVENT_GUID,
    RACE_SETUP_RAW,
    _make_ctx,
    _make_event_data,
)


def _section(section_index, total, laptime):
    return {
        "hook": "ServerPassedRaceSection",
        "timestamp": int(time.time()),
        "data": {
            "CharacterGuid": str(CHAR_GUID),
            "EventGuid": EVENT_GUID,
            "SectionIndex": section_index,
            "TotalTimeSeconds": total,
            "LaptimeSeconds": laptime,
        },
    }


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
class LapReconstructionTests(TestCase):
    async def _setup(self, num_laps=None):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid=CHAR_GUID
        )
        event_data = _make_event_data(state=2)
        if num_laps is not None:
            event_data["RaceSetup"] = {**RACE_SETUP_RAW, "NumLaps": num_laps}
        game_event, _ = await _upsert_game_event(event_data)
        gec = await _upsert_game_event_character(game_event, event_data["Players"][0])
        self._gec_pk = gec.pk
        return player, character

    async def _gec(self):
        return await GameEventCharacter.objects.aget(pk=self._gec_pk)

    async def test_lap_sequence_reconstructs_laps_and_best(self, *mocks):
        """start S0 -> 3 mids -> lap1 S0 -> lap2 S0 = 2 laps, best = min.

        NumLaps=4 (like the live 4-lap quali): a mid-race run keeps crossing
        sections between lap completions, so reconstruction must survive
        them (with NumLaps=0 the first mid-section crossing would finish a
        0-lap run — PR #83's last-waypoint rule)."""
        player, character = await self._setup(num_laps=4)

        # Race start: first section-0 crossing sets first_section, no lap.
        await dispatch(
            "ServerPassedRaceSection",
            _section(0, 23.44, 5221.59),
            player,
            character,
            _make_ctx(),
        )
        gec = await self._gec()
        self.assertIsNotNone(gec.first_section_total_time_seconds)
        self.assertEqual(gec.lap_times, [])
        self.assertEqual(gec.best_lap_time, 0)
        self.assertEqual(gec.laps, 1)

        # Mid-lap sections don't record laps.
        for si, tt, lt in [(1, 29.55, 6.1), (2, 33.60, 10.2), (3, 38.25, 14.8)]:
            await dispatch(
                "ServerPassedRaceSection",
                _section(si, tt, lt),
                player,
                character,
                _make_ctx(),
            )

        # Lap 1 complete (second S0 crossing): lap time = 19.88.
        await dispatch(
            "ServerPassedRaceSection",
            _section(0, 43.32, 19.88),
            player,
            character,
            _make_ctx(),
        )
        gec = await self._gec()
        self.assertEqual(gec.lap_times, [19.88])
        self.assertEqual(gec.best_lap_time, 19.88)
        self.assertEqual(gec.laps, 2)

        # Lap 2 complete: 16.83 — new best.
        await dispatch(
            "ServerPassedRaceSection",
            _section(0, 60.15, 16.83),
            player,
            character,
            _make_ctx(),
        )
        gec = await self._gec()
        self.assertEqual(gec.lap_times, [19.88, 16.83])
        self.assertEqual(gec.best_lap_time, 16.83)
        self.assertEqual(gec.laps, 3)

    async def test_sentinel_laptime_not_recorded(self, *mocks):
        """Countdown-restart sentinel (boot age, ~6300s, > TotalTime) is ignored.

        Live-observed 2026-09-05: start line S0(TotalTime=6.75,
        LaptimeSeconds=6329) arrived with first_section already set from an
        aborted countdown — must not count as a lap."""
        player, character = await self._setup()

        await dispatch(
            "ServerPassedRaceSection",
            _section(0, 23.44, 5221.59),
            player,
            character,
            _make_ctx(),
        )
        await dispatch(
            "ServerPassedRaceSection",
            _section(0, 6.75, 6329.98),
            player,
            character,
            _make_ctx(),
        )
        gec = await self._gec()
        self.assertEqual(gec.lap_times, [])
        self.assertEqual(gec.best_lap_time, 0)
        self.assertEqual(gec.laps, 1)

    async def test_sprint_single_s0_records_no_lap(self, *mocks):
        """Point-to-point event: one S0 crossing = start only, no laps."""
        player, character = await self._setup()

        await dispatch(
            "ServerPassedRaceSection",
            _section(0, 10.0, 10.0),
            player,
            character,
            _make_ctx(),
        )
        gec = await self._gec()
        self.assertEqual(gec.lap_times, [])
        self.assertEqual(gec.laps, 1)
