"""Tests for the SSE event handler module (amc.handlers.events)."""

import asyncio
import time
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase
from django.utils import timezone

from amc.factories import PlayerFactory, CharacterFactory
from amc.handlers import dispatch
from amc.handlers.events import (
    _upsert_game_event,
    _upsert_game_event_character,
    handle_change_event_state,
    handle_passed_race_section,
)
from amc.models import (
    GameEvent,
    GameEventCharacter,
    LapSectionTime,
    RaceSetup,
    ScheduledEvent,
)
from amc.webhook_context import EventContext


def _make_ctx(**kwargs):
    """Create an EventContext with sensible defaults for tests."""
    defaults = dict(
        http_client=None,
        http_client_mod=None,
        discord_client=None,
        treasury_balance=100_000,
        is_rp_mode=False,
        used_shortcut=False,
        active_term=None,
    )
    defaults.update(kwargs)
    return EventContext(**defaults)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

RACE_SETUP_RAW = {
    "NumLaps": 0,
    "Route": {
        "RouteName": "Test Route",
        "Waypoints": [
            {
                "Location": {"X": -254858.0, "Y": 118884.0, "Z": -19609.0},
                "Rotation": {"X": 0.0, "Y": -0.0, "Z": 0.0, "W": 1.0},
                "Scale3D": {"X": 1.0, "Y": 20.0, "Z": 10.0},
            },
            {
                "Location": {"X": -240477.0, "Y": 99544.0, "Z": -19115.0},
                "Rotation": {"X": 0.0, "Y": -0.0, "Z": 0.0, "W": 1.0},
                "Scale3D": {"X": 1.0, "Y": 20.0, "Z": 10.0},
            },
        ],
    },
    "VehicleKeys": [],
    "EngineKeys": [],
}

EVENT_GUID = "5B11926A45D1869C3AA6309F3F564829"
CHAR_GUID = "E603C74946EFF3F8834C9AAB3D0E3181"
PLAYER_ID = "76561198378447512"


def _make_event_data(state=1, players=None):
    """Build a full FMTEvent dict as emitted by the C++ ServerAddEvent hook."""
    if players is None:
        players = [
            {
                "CharacterId": {
                    "UniqueNetId": PLAYER_ID,
                    "CharacterGuid": CHAR_GUID,
                },
                "PlayerName": "testplayer",
                "Rank": 0,
                "SectionIndex": -1,
                "Laps": 0,
                "BestLapTime": 0.0,
                "LastSectionTotalTimeSeconds": 0.0,
                "bFinished": False,
                "bDisqualified": False,
                "bWrongVehicle": False,
                "bWrongEngine": False,
                "LapTimes": [],
                "Reward_Money": {"BaseValue": 0},
            }
        ]

    return {
        "EventGuid": EVENT_GUID,
        "EventName": "Test Event",
        "State": state,
        "OwnerCharacterId": {
            "UniqueNetId": PLAYER_ID,
            "CharacterGuid": CHAR_GUID,
        },
        "RaceSetup": RACE_SETUP_RAW,
        "Players": players,
    }


# ---------------------------------------------------------------------------
# Tests: _upsert_game_event
# ---------------------------------------------------------------------------


class UpsertGameEventTests(TestCase):
    async def test_creates_game_event(self):
        event_data = _make_event_data(state=1)
        game_event, transition = await _upsert_game_event(event_data)

        self.assertIsNotNone(game_event)
        self.assertEqual(game_event.guid, EVENT_GUID)
        self.assertEqual(game_event.name, "Test Event")
        self.assertEqual(game_event.state, 1)
        self.assertIsNone(transition)
        self.assertTrue(await GameEvent.objects.filter(guid=EVENT_GUID).aexists())

    async def test_creates_race_setup(self):
        event_data = _make_event_data()
        await _upsert_game_event(event_data)

        race_setup = await RaceSetup.objects.afirst()
        self.assertIsNotNone(race_setup)
        self.assertEqual(race_setup.config["Route"]["RouteName"], "Test Route")
        self.assertEqual(race_setup.config["NumLaps"], 0)

    async def test_updates_state_with_transition(self):
        event_data = _make_event_data(state=1)
        await _upsert_game_event(event_data)

        event_data["State"] = 2
        game_event, transition = await _upsert_game_event(event_data)

        self.assertEqual(game_event.state, 2)
        self.assertEqual(transition, (1, 2))

    async def test_no_transition_for_same_state(self):
        event_data = _make_event_data(state=1)
        await _upsert_game_event(event_data)

        game_event, transition = await _upsert_game_event(event_data)
        self.assertIsNone(transition)

    async def test_associates_owner_character(self):
        await sync_to_async(CharacterFactory)(
            player__unique_id=int(PLAYER_ID), guid=CHAR_GUID
        )
        event_data = _make_event_data()
        game_event, _ = await _upsert_game_event(event_data)

        self.assertIsNotNone(game_event.owner)
        self.assertEqual(game_event.owner.guid, CHAR_GUID)


# ---------------------------------------------------------------------------
# Tests: _upsert_game_event_character
# ---------------------------------------------------------------------------


class UpsertGameEventCharacterTests(TestCase):
    async def test_creates_game_event_character(self):
        event_data = _make_event_data(state=1)
        game_event, _ = await _upsert_game_event(event_data)

        player_info = event_data["Players"][0]
        gec = await _upsert_game_event_character(game_event, player_info)

        self.assertIsNotNone(gec)
        self.assertEqual(gec.rank, 0)
        self.assertEqual(gec.section_index, -1)
        self.assertFalse(gec.finished)

    async def test_skips_finished_character(self):
        event_data = _make_event_data(state=2)
        game_event, _ = await _upsert_game_event(event_data)

        player_info = event_data["Players"][0]
        player_info["bFinished"] = True
        gec = await _upsert_game_event_character(game_event, player_info)
        self.assertIsNotNone(gec)
        self.assertTrue(gec.finished)

        # Second call should return None (already finished)
        player_info["Rank"] = 1
        result = await _upsert_game_event_character(game_event, player_info)
        self.assertIsNone(result)

    async def test_records_lap_section_time(self):
        event_data = _make_event_data(state=2)
        game_event, _ = await _upsert_game_event(event_data)

        player_info = event_data["Players"][0]
        player_info["SectionIndex"] = 0
        player_info["Laps"] = 1
        player_info["LastSectionTotalTimeSeconds"] = 69.73
        player_info["Rank"] = 1

        gec = await _upsert_game_event_character(game_event, player_info)
        self.assertIsNotNone(gec)

        lst = await LapSectionTime.objects.filter(
            game_event_character=gec
        ).afirst()
        self.assertIsNotNone(lst)
        self.assertEqual(lst.section_index, 0)
        self.assertEqual(lst.total_time_seconds, 69.73)

    async def test_first_section_time_tracking(self):
        event_data = _make_event_data(state=2)
        game_event, _ = await _upsert_game_event(event_data)

        player_info = event_data["Players"][0]
        player_info["SectionIndex"] = 0
        player_info["Laps"] = 1
        player_info["LastSectionTotalTimeSeconds"] = 69.73

        gec = await _upsert_game_event_character(game_event, player_info)
        await gec.arefresh_from_db()
        self.assertEqual(gec.first_section_total_time_seconds, 69.73)

    async def test_first_section_buggy_large_number(self):
        event_data = _make_event_data(state=2)
        game_event, _ = await _upsert_game_event(event_data)

        player_info = event_data["Players"][0]
        player_info["SectionIndex"] = 0
        player_info["Laps"] = 1
        player_info["LastSectionTotalTimeSeconds"] = 99_999_999.0  # buggy value

        gec = await _upsert_game_event_character(game_event, player_info)
        await gec.arefresh_from_db()
        self.assertEqual(gec.first_section_total_time_seconds, 0)


# ---------------------------------------------------------------------------
# Tests: dispatch to event handlers
# ---------------------------------------------------------------------------


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
class EventDispatchTests(TestCase):
    async def test_server_add_event_dispatch(self, mock_get_treasury, mock_get_rp_mode):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid=CHAR_GUID
        )

        event = {
            "hook": "ServerAddEvent",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Event": _make_event_data(state=1),
            },
        }
        ctx = _make_ctx()
        base_pay, subsidy, contract_pay, clawback = await dispatch(
            "ServerAddEvent", event, player, character, ctx
        )

        self.assertEqual(base_pay, 0)
        self.assertEqual(subsidy, 0)
        self.assertTrue(await GameEvent.objects.filter(guid=EVENT_GUID).aexists())

    async def test_server_change_event_state_dispatch(
        self, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid=CHAR_GUID
        )

        # Create initial event
        event_data = _make_event_data(state=1)
        await _upsert_game_event(event_data)

        # Change state to 2
        event_data["State"] = 2
        event = {
            "hook": "ServerChangeEventState",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Event": event_data,
            },
        }
        ctx = _make_ctx()
        base_pay, subsidy, contract_pay, clawback = await dispatch(
            "ServerChangeEventState", event, player, character, ctx
        )

        self.assertEqual(base_pay, 0)
        ge = await GameEvent.objects.aget(guid=EVENT_GUID)
        self.assertEqual(ge.state, 2)

    async def test_server_passed_race_section_dispatch(
        self, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid=CHAR_GUID
        )

        # Create event + character
        event_data = _make_event_data(state=2)
        game_event, _ = await _upsert_game_event(event_data)
        await _upsert_game_event_character(game_event, event_data["Players"][0])

        event = {
            "hook": "ServerPassedRaceSection",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "EventGuid": EVENT_GUID,
                "SectionIndex": 0,
                "TotalTimeSeconds": 69.73,
                "LaptimeSeconds": 69.73,
            },
        }
        ctx = _make_ctx()
        base_pay, subsidy, contract_pay, clawback = await dispatch(
            "ServerPassedRaceSection", event, player, character, ctx
        )

        self.assertEqual(base_pay, 0)
        gec = await GameEventCharacter.objects.filter(
            game_event=game_event, character=character
        ).afirst()
        self.assertIsNotNone(gec)
        self.assertEqual(gec.section_index, 0)
        self.assertEqual(gec.last_section_total_time_seconds, 69.73)

    async def test_server_passed_race_section_creates_lap_time(
        self, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid=CHAR_GUID
        )

        event_data = _make_event_data(state=2)
        game_event, _ = await _upsert_game_event(event_data)

        player_info = event_data["Players"][0]
        player_info["SectionIndex"] = 0
        player_info["Laps"] = 1
        gec = await _upsert_game_event_character(game_event, player_info)

        event = {
            "hook": "ServerPassedRaceSection",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "EventGuid": EVENT_GUID,
                "SectionIndex": 1,
                "TotalTimeSeconds": 142.5,
                "LaptimeSeconds": 72.77,
            },
        }
        ctx = _make_ctx()
        await dispatch("ServerPassedRaceSection", event, player, character, ctx)

        lst = await LapSectionTime.objects.filter(
            game_event_character=gec, section_index=1
        ).afirst()
        self.assertIsNotNone(lst)
        self.assertEqual(lst.total_time_seconds, 142.5)

    async def test_multi_lap_natural_finish_on_final_s0(
        self, mock_get_treasury, mock_get_rp_mode
    ):
        """A 2-lap route finishes on the S0 crossing that completes the
        final lap (live-observed 2026-09-05: both laps drove, LapTimes
        recorded, finished stayed False forever)."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid=CHAR_GUID
        )

        event_data = _make_event_data(state=2)
        event_data["RaceSetup"] = {**RACE_SETUP_RAW, "NumLaps": 2}
        game_event, _ = await _upsert_game_event(event_data)
        await _upsert_game_event_character(game_event, event_data["Players"][0])

        ctx = _make_ctx()

        async def cross(section, total, laptime):
            event = {
                "hook": "ServerPassedRaceSection",
                "timestamp": int(time.time()),
                "data": {
                    "CharacterGuid": str(character.guid),
                    "EventGuid": EVENT_GUID,
                    "SectionIndex": section,
                    "TotalTimeSeconds": total,
                    "LaptimeSeconds": laptime,
                },
            }
            await dispatch("ServerPassedRaceSection", event, player, character, ctx)

        # Start line: sentinel LaptimeSeconds (boot time) is rejected as a lap
        await cross(0, 1.08, 2241.17)
        # Lap 1
        await cross(1, 3.72, 2.63)
        await cross(2, 6.52, 2.80)
        await cross(3, 8.20, 1.68)
        await cross(0, 12.42, 11.33)
        gec = await GameEventCharacter.objects.filter(
            game_event=game_event, character=character
        ).afirst()
        self.assertFalse(gec.finished)

        # Lap 2 completes -> natural finish
        await cross(1, 14.33, 1.92)
        await cross(2, 16.90, 2.57)
        await cross(3, 18.47, 1.57)
        await cross(0, 21.97, 9.55)

        gec = await GameEventCharacter.objects.filter(
            game_event=game_event, character=character
        ).afirst()
        self.assertTrue(gec.finished)
        # laps = 1 in-progress marker + 2 completed laps
        self.assertEqual(gec.laps, 3)
        self.assertEqual(gec.best_lap_time, 9.55)

    async def test_zero_lap_event_does_not_finish_on_s0(
        self, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid=CHAR_GUID
        )

        event_data = _make_event_data(state=2)  # RACE_SETUP_RAW NumLaps=0
        game_event, _ = await _upsert_game_event(event_data)
        await _upsert_game_event_character(game_event, event_data["Players"][0])

        event = {
            "hook": "ServerPassedRaceSection",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "EventGuid": EVENT_GUID,
                "SectionIndex": 0,
                "TotalTimeSeconds": 69.73,
                "LaptimeSeconds": 69.73,
            },
        }
        ctx = _make_ctx()
        await dispatch("ServerPassedRaceSection", event, player, character, ctx)

        gec = await GameEventCharacter.objects.filter(
            game_event=game_event, character=character
        ).afirst()
        self.assertFalse(gec.finished)

    async def test_one_lap_finishes_on_s0_not_last_waypoint(
        self, mock_get_treasury, mock_get_rp_mode
    ):
        """NumLaps>=1 routes finish at the FIRST waypoint (freeman
        2026-09-05): crossing the last waypoint must not finish a 1-lap
        run; the S0 crossing that completes the lap does."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid=CHAR_GUID
        )

        event_data = _make_event_data(state=2)
        event_data["RaceSetup"] = {**RACE_SETUP_RAW, "NumLaps": 1}
        game_event, _ = await _upsert_game_event(event_data)
        await _upsert_game_event_character(game_event, event_data["Players"][0])

        ctx = _make_ctx()

        async def cross(section, total, laptime):
            event = {
                "hook": "ServerPassedRaceSection",
                "timestamp": int(time.time()),
                "data": {
                    "CharacterGuid": str(character.guid),
                    "EventGuid": EVENT_GUID,
                    "SectionIndex": section,
                    "TotalTimeSeconds": total,
                    "LaptimeSeconds": laptime,
                },
            }
            await dispatch("ServerPassedRaceSection", event, player, character, ctx)

        # Start line: sentinel LaptimeSeconds (boot time) is rejected as a lap
        await cross(0, 1.08, 2241.17)
        # Last waypoint of a NumLaps=1 route — NOT the finish checkpoint.
        await cross(1, 3.72, 2.63)
        gec = await GameEventCharacter.objects.filter(
            game_event=game_event, character=character
        ).afirst()
        self.assertFalse(gec.finished)

        # S0 crossing completes lap 1 -> the finish checkpoint -> finished.
        await cross(0, 5.20, 4.12)
        gec = await GameEventCharacter.objects.filter(
            game_event=game_event, character=character
        ).afirst()
        self.assertTrue(gec.finished)
        self.assertEqual(gec.laps, 2)  # start marker + 1 completed lap
        self.assertEqual(gec.lap_times, [4.12])

    async def test_remove_event_noop(self, mock_get_treasury, mock_get_rp_mode):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)

        event = {
            "hook": "ServerRemoveEvent",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "EventGuid": EVENT_GUID,
            },
        }
        ctx = _make_ctx()
        base_pay, _, _, _ = await dispatch(
            "ServerRemoveEvent", event, player, character, ctx
        )
        self.assertEqual(base_pay, 0)

    async def test_join_event_noop(self, mock_get_treasury, mock_get_rp_mode):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)

        event = {
            "hook": "ServerJoinEvent",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "EventGuid": EVENT_GUID,
            },
        }
        ctx = _make_ctx()
        base_pay, _, _, _ = await dispatch(
            "ServerJoinEvent", event, player, character, ctx
        )
        self.assertEqual(base_pay, 0)

    async def test_leave_event_noop(self, mock_get_treasury, mock_get_rp_mode):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)

        event = {
            "hook": "ServerLeaveEvent",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "EventGuid": EVENT_GUID,
            },
        }
        ctx = _make_ctx()
        base_pay, _, _, _ = await dispatch(
            "ServerLeaveEvent", event, player, character, ctx
        )
        self.assertEqual(base_pay, 0)


# ---------------------------------------------------------------------------
# Integration: full event lifecycle via SSE
# ---------------------------------------------------------------------------


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
class EventLifecycleTests(TestCase):
    async def test_add_then_state_change_then_section(
        self, mock_get_treasury, mock_get_rp_mode
    ):
        """Simulate: AddEvent(state=1) → ChangeState(2) → PassedRaceSection → ChangeState(3)."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid=CHAR_GUID
        )
        ctx = _make_ctx()

        # 1. AddEvent (state=1, Ready)
        event_data = _make_event_data(state=1)
        event = {
            "hook": "ServerAddEvent",
            "timestamp": int(time.time()),
            "data": {"CharacterGuid": str(character.guid), "Event": event_data},
        }
        await dispatch("ServerAddEvent", event, player, character, ctx)

        ge = await GameEvent.objects.aget(guid=EVENT_GUID)
        self.assertEqual(ge.state, 1)

        # 2. ChangeState to 2 (In Progress)
        event_data["State"] = 2
        event["hook"] = "ServerChangeEventState"
        event["data"]["Event"] = event_data
        await dispatch("ServerChangeEventState", event, player, character, ctx)

        await ge.arefresh_from_db()
        self.assertEqual(ge.state, 2)

        # 3. PassedRaceSection (first section)
        section_event = {
            "hook": "ServerPassedRaceSection",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "EventGuid": EVENT_GUID,
                "SectionIndex": 0,
                "TotalTimeSeconds": 69.73,
                "LaptimeSeconds": 69.73,
            },
        }
        await dispatch(
            "ServerPassedRaceSection", section_event, player, character, ctx
        )

        gec = await GameEventCharacter.objects.filter(
            game_event__guid=EVENT_GUID, character=character
        ).afirst()
        self.assertEqual(gec.section_index, 0)
        self.assertEqual(gec.last_section_total_time_seconds, 69.73)
        self.assertEqual(gec.first_section_total_time_seconds, 69.73)

        # 4. PassedRaceSection (second section)
        section_event["data"]["SectionIndex"] = 1
        section_event["data"]["TotalTimeSeconds"] = 142.5
        await dispatch(
            "ServerPassedRaceSection", section_event, player, character, ctx
        )

        await gec.arefresh_from_db()
        self.assertEqual(gec.section_index, 1)
        self.assertEqual(gec.last_section_total_time_seconds, 142.5)

        # 5. ChangeState to 3 (Finished)
        event_data["State"] = 3
        event_data["Players"][0]["bFinished"] = True
        event_data["Players"][0]["SectionIndex"] = 1
        event_data["Players"][0]["Laps"] = 1
        event_data["Players"][0]["LastSectionTotalTimeSeconds"] = 142.5
        event["data"]["Event"] = event_data
        await dispatch("ServerChangeEventState", event, player, character, ctx)

        await ge.arefresh_from_db()
        self.assertEqual(ge.state, 3)


# ---------------------------------------------------------------------------
# Tests: scheduled-event association (time-trial and non-time-trial)
# ---------------------------------------------------------------------------


class ScheduledEventAssociationTests(TestCase):
    async def _make_scheduled_event(
        self, time_trial: bool, started_minutes_ago: int, ends_in_minutes: int
    ) -> ScheduledEvent:
        race_setup, _ = await RaceSetup.objects.aget_or_create(
            hash=RaceSetup.calculate_hash(RACE_SETUP_RAW),
            defaults={"config": RACE_SETUP_RAW, "name": "Test Route"},
        )
        now = timezone.now()
        return await ScheduledEvent.objects.acreate(
            name="Scheduled Race",
            start_time=now - timedelta(minutes=started_minutes_ago),
            end_time=now + timedelta(minutes=ends_in_minutes),
            time_trial=time_trial,
            race_setup=race_setup,
        )

    async def test_associates_nontt_scheduled_event(self):
        scheduled_event = await self._make_scheduled_event(
            time_trial=False, started_minutes_ago=30, ends_in_minutes=30
        )
        event_data = _make_event_data(state=1)
        game_event, _ = await _upsert_game_event(event_data)
        await game_event.arefresh_from_db()

        self.assertEqual(game_event.scheduled_event_id, scheduled_event.id)

    async def test_associates_tt_scheduled_event_unchanged(self):
        scheduled_event = await self._make_scheduled_event(
            time_trial=True, started_minutes_ago=30, ends_in_minutes=30
        )
        event_data = _make_event_data(state=1)
        game_event, _ = await _upsert_game_event(event_data)
        await game_event.arefresh_from_db()

        self.assertEqual(game_event.scheduled_event_id, scheduled_event.id)

    async def test_no_association_after_window(self):
        await self._make_scheduled_event(
            time_trial=False, started_minutes_ago=120, ends_in_minutes=-60
        )
        event_data = _make_event_data(state=1)
        game_event, _ = await _upsert_game_event(event_data)
        await game_event.arefresh_from_db()

        self.assertIsNone(game_event.scheduled_event_id)


# ---------------------------------------------------------------------------
# Tests: finish results popup (2→3 transition)
# ---------------------------------------------------------------------------


class FinishResultsPopupTests(TestCase):
    @patch("amc.handlers.events.delay", new=lambda coro, seconds: coro)
    async def test_finish_schedules_results_popup(self):
        await sync_to_async(CharacterFactory)(
            player__unique_id=int(PLAYER_ID), guid=CHAR_GUID
        )

        event_data = _make_event_data(state=2)
        game_event, _ = await _upsert_game_event(event_data)
        await _upsert_game_event_character(game_event, event_data["Players"][0])

        event_data["State"] = 3
        event = {
            "hook": "ServerChangeEventState",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": CHAR_GUID,
                "Event": event_data,
            },
        }
        ctx = _make_ctx(http_client_mod=AsyncMock())

        with patch(
            "amc.handlers.events.show_results_popup", new=AsyncMock()
        ) as mock_popup:
            await handle_change_event_state(event, None, None, ctx)
            await asyncio.sleep(0.05)  # flush the task scheduled by the handler

        mock_popup.assert_awaited_once()
        args = mock_popup.await_args.args
        self.assertEqual(args[0], ctx.http_client_mod)
        self.assertEqual(len(args[1]), 1)  # the one participant

    @patch("amc.handlers.events.delay", new=lambda coro, seconds: coro)
    async def test_no_popup_without_finish_transition(self):
        await _upsert_game_event(_make_event_data(state=1))

        event_data = _make_event_data(state=2)  # 1→2, not a finish
        event = {
            "hook": "ServerChangeEventState",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": CHAR_GUID,
                "Event": event_data,
            },
        }
        ctx = _make_ctx(http_client_mod=AsyncMock())

        with patch(
            "amc.handlers.events.show_results_popup", new=AsyncMock()
        ) as mock_popup:
            await handle_change_event_state(event, None, None, ctx)
            await asyncio.sleep(0.05)

        mock_popup.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests: per-run row resolution + natural finish detection
#
# The game keeps ONE event guid across re-runs (it resets the event to state 1
# between runs), so the backend stores one GameEvent row per run.  Every guid
# lookup must therefore resolve to the LATEST row, and a natural finish (which
# never arrives as a ChangeEventState(3) SSE event — it is a server-internal
# transition) must be derived from the last section crossing.
# ---------------------------------------------------------------------------


class RunRowResolutionTests(TestCase):
    RUN_GUID = "AAAA1111BBBB2222CCCC3333DDDD4444"

    def _section_event(self, section_index, total_time):
        return {
            "hook": "ServerPassedRaceSection",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": CHAR_GUID,
                "EventGuid": self.RUN_GUID,
                "SectionIndex": section_index,
                "TotalTimeSeconds": total_time,
                "LaptimeSeconds": total_time,
            },
        }

    async def _make_two_run_rows(self):
        """Simulate two runs of one game event: run 1 created + started
        (state 2), then the game resets the event to state 1 → a second
        GameEvent row is created for run 2."""
        event_data = _make_event_data(state=1)
        event_data["EventGuid"] = self.RUN_GUID
        run1, _ = await _upsert_game_event(event_data)

        event_data["State"] = 2
        run1, _ = await _upsert_game_event(event_data)

        event_data["State"] = 1  # game reset between runs → new row
        run2, _ = await _upsert_game_event(event_data)
        self.assertNotEqual(run1.pk, run2.pk)

        # Deterministic start_time ordering (auto_now_add resolution).
        await GameEvent.objects.filter(pk=run1.pk).aupdate(
            start_time=timezone.now() - timedelta(minutes=5)
        )

        for run in (run1, run2):
            await _upsert_game_event_character(run, event_data["Players"][0])
        return run1, run2

    async def test_section_events_target_latest_run_row(self):
        run1, run2 = await self._make_two_run_rows()
        ctx = _make_ctx()

        await handle_passed_race_section(
            self._section_event(0, 12.5), None, None, ctx
        )

        gec2 = await GameEventCharacter.objects.filter(game_event=run2).afirst()
        await gec2.arefresh_from_db()
        self.assertEqual(gec2.section_index, 0)
        self.assertEqual(gec2.last_section_total_time_seconds, 12.5)

        # The stale run-1 row must stay untouched.
        gec1 = await GameEventCharacter.objects.filter(game_event=run1).afirst()
        await gec1.arefresh_from_db()
        self.assertEqual(gec1.section_index, -1)
        self.assertEqual(gec1.last_section_total_time_seconds, 0)

    async def test_natural_finish_marks_participant_finished(self):
        """Last section of a single-lap route (NumLaps=0, 2 waypoints → last
        index 1) ⇒ participant finished=True; the event row itself is left
        alone (the game owns the state machine)."""
        event_data = _make_event_data(state=2)
        event_data["EventGuid"] = self.RUN_GUID
        game_event, _ = await _upsert_game_event(event_data)
        await _upsert_game_event_character(game_event, event_data["Players"][0])
        ctx = _make_ctx()

        await handle_passed_race_section(
            self._section_event(0, 60.0), None, None, ctx
        )
        await handle_passed_race_section(
            self._section_event(1, 142.5), None, None, ctx
        )

        gec = await GameEventCharacter.objects.filter(game_event=game_event).afirst()
        await gec.arefresh_from_db()
        self.assertTrue(gec.finished)
        self.assertEqual(gec.last_section_total_time_seconds, 142.5)
        self.assertEqual(gec.first_section_total_time_seconds, 60.0)
        # net_time is a DB-generated column: last - first.
        self.assertAlmostEqual(gec.net_time, 82.5, places=5)

        await game_event.arefresh_from_db()
        self.assertEqual(game_event.state, 2)  # untouched by the handler

    async def test_multilap_route_does_not_natural_finish(self):
        """Multi-lap routes carry no reliable lap count on the section stream,
        so crossing the last section of a lap must NOT mark finished."""
        event_data = _make_event_data(state=2)
        event_data["EventGuid"] = self.RUN_GUID
        event_data["RaceSetup"] = {**RACE_SETUP_RAW, "NumLaps": 3}
        game_event, _ = await _upsert_game_event(event_data)
        await _upsert_game_event_character(game_event, event_data["Players"][0])
        ctx = _make_ctx()

        await handle_passed_race_section(
            self._section_event(1, 142.5), None, None, ctx
        )

        gec = await GameEventCharacter.objects.filter(game_event=game_event).afirst()
        await gec.arefresh_from_db()
        self.assertFalse(gec.finished)

    async def test_finished_participant_ignores_late_sections(self):
        """Delayed SSE bursts re-deliver sections after the finish — a
        finished participant must not be updated again."""
        event_data = _make_event_data(state=2)
        event_data["EventGuid"] = self.RUN_GUID
        game_event, _ = await _upsert_game_event(event_data)
        await _upsert_game_event_character(game_event, event_data["Players"][0])
        ctx = _make_ctx()

        await handle_passed_race_section(
            self._section_event(0, 60.0), None, None, ctx
        )
        await handle_passed_race_section(
            self._section_event(1, 142.5), None, None, ctx
        )
        # Straggler re-delivery with a different total must be ignored.
        await handle_passed_race_section(
            self._section_event(1, 999.0), None, None, ctx
        )

        gec = await GameEventCharacter.objects.filter(game_event=game_event).afirst()
        await gec.arefresh_from_db()
        self.assertTrue(gec.finished)
        self.assertEqual(gec.last_section_total_time_seconds, 142.5)
        lst = await LapSectionTime.objects.filter(
            game_event_character=gec, section_index=1
        ).afirst()
        self.assertEqual(lst.total_time_seconds, 142.5)

    async def test_ownerless_add_event_creates_auto_event(self):
        """Backend-posted auto TTs carry OwnerCharacterId.UniqueNetId == "" —
        the upsert must not crash on the BigInteger lookup and must still
        create the row with owner=None / auto_created=True."""
        event_data = _make_event_data(state=1)
        event_data["EventGuid"] = self.RUN_GUID
        event_data["OwnerCharacterId"] = {"UniqueNetId": "", "CharacterGuid": ""}

        game_event, transition = await _upsert_game_event(event_data)

        self.assertIsNotNone(game_event)
        self.assertIsNone(game_event.owner_id)
        self.assertTrue(game_event.auto_created)
        self.assertIsNone(transition)
