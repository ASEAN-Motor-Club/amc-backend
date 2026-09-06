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
    handle_add_event,
    handle_change_event_state,
    handle_join_event,
    handle_leave_event,
    handle_passed_race_section,
)
from amc.models import (
    GameEvent,
    GameEventCharacter,
    LapSectionTime,
    RaceSetup,
    ScheduledEvent,
)
from amc.events import print_results
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

    async def test_start_payload_writes_wrong_flags(self):
        """The state-2 start payload is the game's race-start evaluation —
        its verdict is authoritative (prod: run 1 in a wrong vehicle/engine)."""
        event_data = _make_event_data(state=2)
        player_info = event_data["Players"][0]
        player_info["bWrongVehicle"] = True
        player_info["bWrongEngine"] = True

        game_event, _ = await _upsert_game_event(event_data)
        gec = await _upsert_game_event_character(game_event, player_info)
        await gec.arefresh_from_db()
        self.assertTrue(gec.wrong_vehicle)
        self.assertTrue(gec.wrong_engine)

    async def test_restart_payload_never_writes_wrong_flags(self):
        """Regression (prod 2026-09-06, guid 0EF8F49D…): the state-1 reset
        payload re-sends the game's STICKY prior-run verdict, so a re-run
        after swapping to a legal vehicle was logged with the previous
        run's Wrong Engine/Vehicle flags.  Flags may only be written by
        the state-2 start payload, on create and update alike."""
        # Run 1: started (and flagged) in a wrong vehicle/engine.
        run1_data = _make_event_data(state=2)
        run1_player = run1_data["Players"][0]
        run1_player["bWrongVehicle"] = True
        run1_player["bWrongEngine"] = True
        run1_event, _ = await _upsert_game_event(run1_data)
        run1_gec = await _upsert_game_event_character(run1_event, run1_player)
        await run1_gec.arefresh_from_db()
        self.assertTrue(run1_gec.wrong_vehicle)

        # Restart while still carrying the stale verdict: creates the new
        # run's row + participant but must NOT write the flags.
        restart_data = _make_event_data(state=1, players=[dict(run1_player)])
        run2_event, _ = await _upsert_game_event(restart_data)
        self.assertIsNotNone(run2_event)
        self.assertNotEqual(run2_event.pk, run1_event.pk)
        run2_gec = await _upsert_game_event_character(
            run2_event, restart_data["Players"][0]
        )
        await run2_gec.arefresh_from_db()
        self.assertFalse(run2_gec.wrong_vehicle)
        self.assertFalse(run2_gec.wrong_engine)

        # Run 2 starts in the now-legal vehicle: the fresh state-2
        # evaluation wins, and run 1's verdict is untouched.
        run2_start = _make_event_data(
            state=2,
            players=[dict(run1_player, bWrongVehicle=False, bWrongEngine=False)],
        )
        started_event, _ = await _upsert_game_event(run2_start)
        self.assertEqual(started_event.pk, run2_event.pk)
        await _upsert_game_event_character(started_event, run2_start["Players"][0])
        await run2_gec.arefresh_from_db()
        self.assertFalse(run2_gec.wrong_vehicle)
        self.assertFalse(run2_gec.wrong_engine)
        await run1_gec.arefresh_from_db()
        self.assertTrue(run1_gec.wrong_vehicle)
        self.assertTrue(run1_gec.wrong_engine)

    async def test_snapshot_lap_data_never_written(self):
        """Regression (prod 2026-09-06, guid 80A047D1…): the game's
        per-player LapTimes array ACCUMULATES across re-runs and is never
        cleared game-side (live lobby carried 12-14 entries for a 5-lap
        event), so payload-driven writes contaminated fresh run rows and
        the popup showed L1..L12.  Lap data is reconstruction-only; the
        per-run ``Laps`` counter is still honored."""
        event_data = _make_event_data(state=2)
        game_event, _ = await _upsert_game_event(event_data)

        player_info = event_data["Players"][0]
        player_info["LapTimes"] = [1.0] * 12  # accumulated game-side garbage
        player_info["BestLapTime"] = 9.9
        player_info["Laps"] = 5

        gec = await _upsert_game_event_character(game_event, player_info)
        await gec.arefresh_from_db()
        self.assertEqual(gec.lap_times, [])
        self.assertEqual(gec.best_lap_time, 0)
        self.assertEqual(gec.laps, 5)

        # The update path can't stomp reconstructed laps either.
        gec.lap_times = [7.5]
        await gec.asave(update_fields=["lap_times"])
        await _upsert_game_event_character(game_event, player_info)
        await gec.arefresh_from_db()
        self.assertEqual(gec.lap_times, [7.5])

    async def test_finished_participant_flags_frozen_even_before_row_update(self):
        """A finished player exiting their vehicle must never pick up wrong
        flags — even when the DB row doesn't know they finished yet (the
        payload does): the verdict window closes at bFinished."""
        event_data = _make_event_data(state=2)
        game_event, _ = await _upsert_game_event(event_data)

        player_info = event_data["Players"][0]
        player_info["bWrongVehicle"] = False
        gec = await _upsert_game_event_character(game_event, player_info)
        self.assertFalse(gec.wrong_vehicle)

        # Player finishes; the row isn't marked yet, but the payload says
        # bFinished=True while they're already out of the vehicle.
        player_info["bFinished"] = True
        player_info["bWrongVehicle"] = True
        player_info["bWrongEngine"] = True
        await _upsert_game_event_character(game_event, player_info)

        await gec.arefresh_from_db()
        self.assertTrue(gec.finished)
        self.assertFalse(gec.wrong_vehicle)
        self.assertFalse(gec.wrong_engine)


# ---------------------------------------------------------------------------
# Tests: live reconciliation (ServerJoinEvent + start transition)
# ---------------------------------------------------------------------------


LATE_JOINER_ID = "76561199000000001"
LATE_JOINER_GUID = "AAA1C74946EFF3F8834C9AAB3D0E3181"


def _make_joiner(**overrides):
    player = {
        "CharacterId": {"UniqueNetId": LATE_JOINER_ID, "CharacterGuid": LATE_JOINER_GUID},
        "PlayerName": "latejoiner",
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
    player.update(overrides)
    return player


@patch("amc.handlers.events.get_event_state", new_callable=AsyncMock)
class JoinEventReconcileTests(TestCase):
    async def test_join_records_player_between_snapshots(self, mock_get_live):
        """Joiners who arrive after the last Add/ChangeEventState snapshot
        were never recorded at all (prod: 3 of 10 lobby members had no
        rows).  The join now reconciles from the mod's live event state."""
        event_data = _make_event_data(state=1)
        game_event, _ = await _upsert_game_event(event_data)

        # Lobby live state: the joiner shows wrong flags purely because
        # they're on foot — lobby values must NOT become flags.
        joiner = _make_joiner(bWrongVehicle=True, bWrongEngine=True, LapTimes=[11.0, 12.0])
        mock_get_live.return_value = dict(event_data, Players=[joiner])

        ctx = _make_ctx(http_client_mod=object())
        result = await handle_join_event(
            {"data": {"EventGuid": EVENT_GUID}}, None, None, ctx
        )
        self.assertEqual(result, (0, 0, 0, 0))

        row = await GameEventCharacter.objects.filter(
            character__player__unique_id=LATE_JOINER_ID
        ).afirst()
        self.assertIsNotNone(row)
        self.assertFalse(row.wrong_vehicle)
        self.assertFalse(row.wrong_engine)
        self.assertEqual(row.lap_times, [])
        mock_get_live.assert_awaited_once()

    async def test_join_during_race_marks_wrong_engine(self, mock_get_live):
        """Prod (freeman's event): a wrong-engine participant was unmarked
        because no snapshot ever carried their verdict.  A join reconcile
        while racing records the game's live evaluation."""
        event_data = _make_event_data(state=2)
        await _upsert_game_event(event_data)

        joiner = _make_joiner(bWrongEngine=True)
        mock_get_live.return_value = dict(event_data, Players=[joiner])

        await handle_join_event({"data": {"EventGuid": EVENT_GUID}}, None, None, _make_ctx(http_client_mod=object()))

        row = await GameEventCharacter.objects.filter(
            character__player__unique_id=LATE_JOINER_ID
        ).afirst()
        self.assertIsNotNone(row)
        self.assertTrue(row.wrong_engine)
        self.assertFalse(row.wrong_vehicle)

    async def test_join_does_not_update_finished_participant(self, mock_get_live):
        """Finished participants stay frozen: they exit the vehicle right
        after completing the course and the live state then reads them as
        wrong (prod lobby: EVERYONE shows wrong while on foot)."""
        event_data = _make_event_data(state=2)
        game_event, _ = await _upsert_game_event(event_data)

        finisher = _make_joiner(bFinished=True)
        gec = await _upsert_game_event_character(game_event, finisher)
        self.assertTrue(gec.finished)

        mock_get_live.return_value = dict(
            event_data, Players=[_make_joiner(bFinished=True, bWrongVehicle=True, bWrongEngine=True)]
        )
        await handle_join_event({"data": {"EventGuid": EVENT_GUID}}, None, None, _make_ctx(http_client_mod=object()))

        await gec.arefresh_from_db()
        self.assertTrue(gec.finished)
        self.assertFalse(gec.wrong_vehicle)
        self.assertFalse(gec.wrong_engine)

    async def test_join_survives_live_fetch_failure(self, mock_get_live):
        mock_get_live.side_effect = Exception("mod unreachable")
        await _upsert_game_event(_make_event_data(state=1))

        result = await handle_join_event(
            {"data": {"EventGuid": EVENT_GUID}}, None, None, _make_ctx(http_client_mod=object())
        )
        self.assertEqual(result, (0, 0, 0, 0))
        # No rows were created for the joiner, and the handler didn't raise.
        self.assertFalse(
            await GameEventCharacter.objects.filter(
                character__player__unique_id=LATE_JOINER_ID
            ).aexists()
        )

    async def test_start_transition_reconciles_omitted_players(self, mock_get_live):
        """The start snapshot can omit mid-countdown joiners entirely; the
        1→2 transition re-syncs from live state so nobody starts
        unrecorded (prod: wrong-engine participant unmarked)."""
        await handle_add_event(
            {"data": {"Event": _make_event_data(state=1, players=[])}},
            None,
            None,
            _make_ctx(http_client_mod=object()),
        )

        start_payload = _make_event_data(state=2, players=[])
        racer = _make_joiner(bWrongEngine=True)
        mock_get_live.return_value = dict(start_payload, Players=[racer])

        await handle_change_event_state(
            {"data": {"Event": start_payload}}, None, None, _make_ctx(http_client_mod=object())
        )

        row = await GameEventCharacter.objects.filter(
            character__player__unique_id=LATE_JOINER_ID
        ).afirst()
        self.assertIsNotNone(row)
        self.assertTrue(row.wrong_engine)

    async def test_start_transition_skips_reconcile_on_state_race(self, mock_get_live):
        """If the live fetch raced a concurrent restart and still reports
        state 1, the reconcile must not create a phantom per-run row."""
        await handle_add_event(
            {"data": {"Event": _make_event_data(state=1, players=[])}},
            None,
            None,
            _make_ctx(http_client_mod=object()),
        )

        start_payload = _make_event_data(state=2, players=[])
        mock_get_live.return_value = dict(start_payload, Players=[_make_joiner()], State=1)

        await handle_change_event_state(
            {"data": {"Event": start_payload}}, None, None, _make_ctx(http_client_mod=object())
        )

        self.assertFalse(
            await GameEventCharacter.objects.filter(
                character__player__unique_id=LATE_JOINER_ID
            ).aexists()
        )
        self.assertEqual(await GameEvent.objects.filter(guid=EVENT_GUID).acount(), 1)


ROSTER_GUID = "DDD1C74946EFF3F8834C9AAB3D0E3181"


def _roster_member(slot, name, **overrides):
    member = _make_joiner(
        CharacterId={
            "UniqueNetId": f"76561199000000{slot:03d}",
            "CharacterGuid": f"{chr(65 + slot)}{ROSTER_GUID[1:]}",
        },
        PlayerName=name,
    )
    member.update(overrides)
    return member


@patch("amc.handlers.events.get_event_state", new_callable=AsyncMock)
class LeaveReconcileTests(TestCase):
    """Events are only joinable while Ready (freeman 2026-09-06): joiners
    AND leavers both happen pre-race, so the roster reconciles prune
    never-raced rows of characters no longer in the event."""

    async def test_leave_prunes_never_raced_participant(self, mock_get_live):
        event_data = _make_event_data(state=1)
        game_event, _ = await _upsert_game_event(event_data)
        stayer = _roster_member(1, "stayer")
        leaver = _roster_member(2, "leaver")
        for member in (stayer, leaver):
            await _upsert_game_event_character(game_event, member)

        # Live roster after the leave: only the stayer remains.
        mock_get_live.return_value = dict(event_data, Players=[stayer])
        await handle_leave_event(
            {"data": {"EventGuid": EVENT_GUID}},
            None,
            None,
            _make_ctx(http_client_mod=object()),
        )

        self.assertFalse(
            await GameEventCharacter.objects.filter(
                game_event=game_event, character__guid=leaver["CharacterId"]["CharacterGuid"]
            ).aexists()
        )
        self.assertTrue(
            await GameEventCharacter.objects.filter(
                game_event=game_event, character__guid=stayer["CharacterId"]["CharacterGuid"]
            ).aexists()
        )

    async def test_leave_never_deletes_raced_or_finished_rows(self, mock_get_live):
        event_data = _make_event_data(state=1)
        game_event, _ = await _upsert_game_event(event_data)
        finisher = _roster_member(3, "finisher", bFinished=True)
        finished_row = await _upsert_game_event_character(game_event, finisher)
        self.assertTrue(finished_row.finished)
        raced_dnf = _roster_member(4, "raceddnf")
        raced_row = await _upsert_game_event_character(game_event, raced_dnf)
        raced_row.laps = 1
        await raced_row.asave(update_fields=["laps"])

        # Both absent from the live roster — but they raced; never delete.
        mock_get_live.return_value = dict(event_data, Players=[])
        await handle_leave_event(
            {"data": {"EventGuid": EVENT_GUID}},
            None,
            None,
            _make_ctx(http_client_mod=object()),
        )

        self.assertTrue(
            await GameEventCharacter.objects.filter(pk=finished_row.pk).aexists()
        )
        self.assertTrue(
            await GameEventCharacter.objects.filter(pk=raced_row.pk).aexists()
        )

    async def test_start_transition_prunes_pre_race_phantoms(self, mock_get_live):
        """A Ready-state joiner whose leave SSE was missed must not enter
        the race roster as a phantom DNF — the start reconcile prunes
        never-raced rows absent from the live roster."""
        await handle_add_event(
            {"data": {"Event": _make_event_data(state=1, players=[])}},
            None,
            None,
            _make_ctx(http_client_mod=object()),
        )
        run_row = (
            await GameEvent.objects.filter(guid=EVENT_GUID)
            .order_by("-start_time")
            .afirst()
        )
        phantom = _roster_member(5, "phantom")
        await _upsert_game_event_character(run_row, phantom)

        racer = _roster_member(6, "racer", bWrongEngine=True)
        start_payload = _make_event_data(state=2, players=[])
        mock_get_live.return_value = dict(start_payload, Players=[racer])
        await handle_change_event_state(
            {"data": {"Event": start_payload}}, None, None, _make_ctx(http_client_mod=object())
        )

        self.assertFalse(
            await GameEventCharacter.objects.filter(
                game_event=run_row, character__guid=phantom["CharacterId"]["CharacterGuid"]
            ).aexists()
        )
        racer_row = await GameEventCharacter.objects.filter(
            game_event=run_row, character__guid=racer["CharacterId"]["CharacterGuid"]
        ).afirst()
        self.assertIsNotNone(racer_row)
        self.assertTrue(racer_row.wrong_engine)

    async def test_malformed_payload_missing_players_prunes_nothing(self, mock_get_live):
        """A truncated live payload without an explicit Players roster must
        not be mistaken for an empty roster — prune nothing, sync nothing
        (freeman: consider edge cases now that we depend on pulling)."""
        event_data = _make_event_data(state=1)
        game_event, _ = await _upsert_game_event(event_data)
        member = _roster_member(7, "innocent")
        await _upsert_game_event_character(game_event, member)

        malformed = dict(event_data)
        del malformed["Players"]
        mock_get_live.return_value = malformed
        await handle_leave_event(
            {"data": {"EventGuid": EVENT_GUID}},
            None,
            None,
            _make_ctx(http_client_mod=object()),
        )

        self.assertTrue(
            await GameEventCharacter.objects.filter(
                game_event=game_event, character__guid=member["CharacterId"]["CharacterGuid"]
            ).aexists()
        )


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

    async def test_backend_finish_when_all_participants_finished(
        self, mock_get_treasury, mock_get_rp_mode
    ):
        """The game never announces completion — when the section stream
        says every participant finished, the backend performs the finish:
        state 3 + results popup + EXP (freeman 2026-09-05)."""
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

        with patch(
            "amc.handlers.events._show_finish_results", new_callable=AsyncMock
        ) as mock_popup, patch(
            "amc.handlers.events._reward_event_exp", new_callable=AsyncMock
        ) as mock_exp, patch(
            "amc.handlers.events.delay", new=lambda coro, seconds: coro
        ), patch(
            "amc.handlers.events._throttled_update_embed", new_callable=AsyncMock
        ):
            # Start line + lap 1 (not finished yet).
            await cross(0, 1.08, 2241.17)
            await cross(1, 3.72, 2.63)
            await cross(0, 12.42, 11.33)
            await game_event.arefresh_from_db()
            self.assertEqual(game_event.state, 2)
            mock_popup.assert_not_awaited()

            # Lap 2 completes -> all participants finished -> backend finish.
            await cross(1, 14.33, 1.92)
            await cross(0, 21.97, 9.55)
            # Let the scheduled _maybe_finish_event task (and its nested
            # popup/EXP tasks) run to completion.
            pending = [
                t for t in asyncio.all_tasks() if t is not asyncio.current_task()
            ]
            if pending:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True), timeout=2
                )

        await game_event.arefresh_from_db()
        self.assertEqual(game_event.state, 3)
        mock_popup.assert_awaited_once()
        mock_exp.assert_awaited_once()

    async def test_backend_finish_waits_for_all_participants(
        self, mock_get_treasury, mock_get_rp_mode
    ):
        """A participant who has not finished blocks the backend finish."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid=CHAR_GUID
        )
        other = await sync_to_async(CharacterFactory)(player=player)

        event_data = _make_event_data(state=2)
        event_data["RaceSetup"] = {**RACE_SETUP_RAW, "NumLaps": 1}
        game_event, _ = await _upsert_game_event(event_data)
        await _upsert_game_event_character(game_event, event_data["Players"][0])
        # A second participant who never completes (valid GEC via the upsert
        # helper — the model requires rank etc.).
        other_info = {
            "CharacterId": {
                "UniqueNetId": "76561190000000001",
                "CharacterGuid": str(other.guid),
            },
            "PlayerName": "other",
        }
        await _upsert_game_event_character(game_event, other_info)

        ctx = _make_ctx()
        event = {
            "hook": "ServerPassedRaceSection",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "EventGuid": EVENT_GUID,
                "SectionIndex": 0,
                "TotalTimeSeconds": 10.0,
                "LaptimeSeconds": 10.0,
            },
        }
        with patch(
            "amc.handlers.events._show_finish_results", new_callable=AsyncMock
        ) as mock_popup, patch(
            "amc.handlers.events.delay", new=lambda coro, seconds: coro
        ):
            # First S0 = start marker; second S0 completes lap 1 -> finished.
            await dispatch("ServerPassedRaceSection", event, player, character, ctx)
            event["data"]["TotalTimeSeconds"] = 20.0
            event["data"]["LaptimeSeconds"] = 10.0
            await dispatch("ServerPassedRaceSection", event, player, character, ctx)

        await game_event.arefresh_from_db()
        self.assertEqual(game_event.state, 2)  # other participant still racing
        mock_popup.assert_not_awaited()

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


class LapBreakdownTests(TestCase):
    """print_results per-lap breakdown (pure formatting, no DB rows)."""

    @staticmethod
    def _participant(name, lap_times=None, net_time=120.0, finished=True):
        from types import SimpleNamespace

        return SimpleNamespace(
            character=SimpleNamespace(name=name, guid="G" * 32),
            net_time=net_time,
            finished=finished,
            wrong_engine=False,
            wrong_vehicle=False,
            best_lap_time=min(lap_times) if lap_times else 0,
            lap_times=list(lap_times or []),
        )

    def test_breakdown_rows_with_best_delta_and_position(self):
        p1 = self._participant("yuyou", [19.44, 17.99, 18.24])
        p2 = self._participant("freeman", [20.10, 18.55, 18.90])
        text = print_results([p1, p2])

        self.assertIn("<Title>Lap breakdown</>", text)
        # Row format: lap, lap time, delta to own best (BEST on fastest),
        # in-lap position among all participants' same-index laps.
        self.assertIn("  L1     19.440s     +1.450  P1", text)
        self.assertIn("  L2     17.990s       BEST  P1", text)
        self.assertIn("  L1     20.100s     +1.550  P2", text)
        self.assertIn("  L2     18.550s       BEST  P2", text)
        self.assertIn("  L3     18.900s     +0.350  P2", text)

    def test_breakdown_omitted_when_no_laps(self):
        p = self._participant("sprinter", [])
        text = print_results([p])
        self.assertNotIn("Lap breakdown", text)
        self.assertIn("#01: ", text)  # legacy results layout unchanged

    def test_breakdown_skips_sentinel_and_excludes_lapless(self):
        p1 = self._participant("yuyou", [6329.98, 17.99])
        p2 = self._participant("noLaps", [])
        text = print_results([p1, p2])

        self.assertNotIn("6329", text)  # boot-age sentinel never rendered
        self.assertIn("  L1     17.990s       BEST", text)
        # noLaps appears only in the results table, not the breakdown.
        self.assertEqual(text.count("noLaps"), 1)
        self.assertEqual(text.count("yuyou"), 2)

    def test_single_participant_rows_have_no_position(self):
        p = self._participant("solo", [11.32, 9.54])
        text = print_results([p])
        self.assertIn("  L1     11.320s     +1.780", text)
        self.assertIn("  L2      9.540s       BEST", text)
        self.assertNotIn("  P1", text)  # no in-lap position for solo runs


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
        index 1) ⇒ participant finished=True, and since the game never
        announces completion the backend performs the finish itself: state
        3 + results popup + EXP (freeman 2026-09-05)."""
        event_data = _make_event_data(state=2)
        event_data["EventGuid"] = self.RUN_GUID
        game_event, _ = await _upsert_game_event(event_data)
        await _upsert_game_event_character(game_event, event_data["Players"][0])
        ctx = _make_ctx()

        with patch(
            "amc.handlers.events._show_finish_results", new_callable=AsyncMock
        ) as mock_popup, patch(
            "amc.handlers.events._reward_event_exp", new_callable=AsyncMock
        ), patch(
            "amc.handlers.events.delay", new=lambda coro, seconds: coro
        ), patch(
            "amc.handlers.events._throttled_update_embed", new_callable=AsyncMock
        ):
            await handle_passed_race_section(
                self._section_event(0, 60.0), None, None, ctx
            )
            await handle_passed_race_section(
                self._section_event(1, 142.5), None, None, ctx
            )
            # Let the scheduled _maybe_finish_event task (and its nested
            # popup/EXP tasks) run to completion.
            pending = [
                t for t in asyncio.all_tasks() if t is not asyncio.current_task()
            ]
            if pending:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True), timeout=2
                )

        gec = await GameEventCharacter.objects.filter(game_event=game_event).afirst()
        await gec.arefresh_from_db()
        self.assertTrue(gec.finished)
        self.assertEqual(gec.last_section_total_time_seconds, 142.5)
        self.assertEqual(gec.first_section_total_time_seconds, 60.0)
        # net_time is a DB-generated column: last - first.
        self.assertAlmostEqual(gec.net_time, 82.5, places=5)

        await game_event.arefresh_from_db()
        self.assertEqual(game_event.state, 3)  # backend-detected finish
        mock_popup.assert_awaited_once()

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

    async def test_one_lap_finishes_on_s0_not_last_waypoint(self):
        """NumLaps>=1 routes finish at the FIRST waypoint (freeman
        2026-09-05): crossing the last waypoint must not finish a 1-lap
        run; the S0 crossing that completes the lap does."""
        event_data = _make_event_data(state=2)
        event_data["EventGuid"] = self.RUN_GUID
        event_data["RaceSetup"] = {**RACE_SETUP_RAW, "NumLaps": 1}
        game_event, _ = await _upsert_game_event(event_data)
        await _upsert_game_event_character(game_event, event_data["Players"][0])
        ctx = _make_ctx()

        # Start line.
        await handle_passed_race_section(
            self._section_event(0, 60.0), None, None, ctx
        )
        # Last waypoint of a NumLaps=1 route — NOT the finish checkpoint.
        await handle_passed_race_section(
            self._section_event(1, 142.5), None, None, ctx
        )
        gec = await GameEventCharacter.objects.filter(game_event=game_event).afirst()
        await gec.arefresh_from_db()
        self.assertFalse(gec.finished)

        # S0 crossing completes lap 1 -> the finish checkpoint -> finished.
        await handle_passed_race_section(
            self._section_event(0, 300.0), None, None, ctx
        )
        gec = await GameEventCharacter.objects.filter(game_event=game_event).afirst()
        await gec.arefresh_from_db()
        self.assertTrue(gec.finished)
        self.assertEqual(gec.laps, 2)  # start marker + 1 completed lap
        self.assertEqual(gec.lap_times, [300.0])

    async def test_fresh_run_clears_snapshot_carried_laps(self):
        """A re-run's start snapshot carries the PRIOR run's LapTimes/
        BestLapTime — the run's first crossing must wipe them so the lap
        breakdown shows only this run's laps (live evidence 2026-09-05:
        GE31 held 4 stale entries from run 1)."""
        event_data = _make_event_data(state=2)
        event_data["EventGuid"] = self.RUN_GUID
        game_event, _ = await _upsert_game_event(event_data)
        gec = await _upsert_game_event_character(
            game_event, event_data["Players"][0]
        )
        await GameEventCharacter.objects.filter(pk=gec.pk).aupdate(
            lap_times=[11.32, 9.54], best_lap_time=9.54, laps=0
        )
        ctx = _make_ctx()

        # First crossing of the new run: the start-line marker.
        await handle_passed_race_section(
            self._section_event(0, 60.0), None, None, ctx
        )

        gec = await GameEventCharacter.objects.filter(game_event=game_event).afirst()
        await gec.arefresh_from_db()
        self.assertEqual(gec.laps, 1)
        self.assertEqual(gec.lap_times, [])  # stale prior-run laps wiped
        self.assertEqual(gec.best_lap_time, 0)
        self.assertEqual(gec.first_section_total_time_seconds, 60.0)

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

    async def test_new_run_closes_out_previous_unfinished_row(self):
        """The game resets to state 1 for a new run without announcing the
        previous run's finish — the prior In-Progress row is assumed
        Finished before the new run's row is created (freeman 2026-09-05)."""
        event_data = _make_event_data(state=2)
        event_data["EventGuid"] = self.RUN_GUID
        run1, _ = await _upsert_game_event(event_data)

        new_data = _make_event_data(state=1)
        new_data["EventGuid"] = self.RUN_GUID
        run2, transition = await _upsert_game_event(new_data)

        self.assertIsNotNone(run2)
        self.assertNotEqual(run2.pk, run1.pk)
        self.assertEqual(run2.state, 1)
        self.assertIsNone(transition)
        await run1.arefresh_from_db()
        self.assertEqual(run1.state, 3)  # assumed Finished

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
