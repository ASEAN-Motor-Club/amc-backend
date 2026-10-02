"""Tests for the illegal-race police trigger (handlers/tt_police.py) and
the 0-lap-only rotation filter (events.post_random_events).

Covers the contract agreed for the Yuuka 2026-09-26 request:

* every non-DQ'd participant gets the suspect badge WITHOUT a wanted
  star (Yuuka 2026-09-27: "badge should still exist, but no star" — the
  Wanted row is what despawns modded cars); a missing Character row
  skips that player and no Wanted row is ever created
* the checkpoint-gated announcement fires only while the event is still live AND
  racing (state 2); vanished / between-run-reset events stay silent
* rotation candidates are restricted to race setups with NumLaps == 0
"""

import re
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

import amc.handlers.tt_police as tt_police  # noqa: F401  (referenced in patches)
from amc.events import post_random_events
from amc.handlers.tt_police import (
    RACE_ALERT_MESSAGE,
    announce_illegal_race,
    grant_race_wanted,
)
from amc.config import UNDERGROUND_CHAMPIONSHIP_NAME
from amc.models import Championship, Character, GameEvent, RaceSetup, ScheduledEvent
from amc.test_auto_tt import (  # noqa: F401  (fixtures shared)
    FakeModClient,
    _race_config,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_alert_state():
    """Isolate the module-level alert registries between tests."""
    tt_police._alert_tasks.clear()
    tt_police._announced_race_guids.clear()
    tt_police._alert_targets.clear()
    tt_police._announce_locks.clear()
    yield
    tt_police._alert_tasks.clear()
    tt_police._announced_race_guids.clear()
    tt_police._alert_targets.clear()
    tt_police._announce_locks.clear()


def _player(guid, unique_id, name):
    return {
        "CharacterId": {"CharacterGuid": guid, "UniqueNetId": unique_id},
        "PlayerName": name,
    }


async def _make_character(name, player_id, guid):
    return await Character.objects.aget_or_create_character_player(
        name, player_id, character_guid=guid
    )


@pytest.mark.asyncio
@patch("amc.criminals.create_or_refresh_wanted", new_callable=AsyncMock)
async def test_grant_wanted_all_online_participants(grant_mock, db):
    from amc.criminals import WANTED_ORIGIN_EVENT_RACE

    await _make_character("Alice", 1, "GUIDPOL000000000000000000000001")
    await _make_character("Bob", 2, "GUIDPOL000000000000000000000002")
    event = await sync_to_async(GameEvent.objects.create)(
        guid="GUIDPOL000000000000000000000E", name="Police Test [TT-270]", state=2
    )
    for guid in (
        "GUIDPOL000000000000000000000001",
        "GUIDPOL000000000000000000000002",
    ):
        char = await Character.objects.aget(guid=guid)
        char.last_online = timezone.now()
        await char.asave()
        await sync_to_async(event.participants.create)(character=char, rank=0)
    granted = await grant_race_wanted(object(), event)
    assert sorted(granted) == [
        "GUIDPOL000000000000000000000001",
        "GUIDPOL000000000000000000000002",
    ]
    assert grant_mock.await_count == 2
    kwargs = grant_mock.await_args_list[0].kwargs
    assert kwargs["origin"] == WANTED_ORIGIN_EVENT_RACE
    assert kwargs["mod_vehicles_allowed"] is True
    assert kwargs["bounty"] == 0


@pytest.mark.asyncio
@patch("amc.criminals.create_or_refresh_wanted", new_callable=AsyncMock)
async def test_grant_wanted_skips_offline(grant_mock, db):
    alice, _, _, _ = await _make_character(
        "Alice", 1, "GUIDPOL000000000000000000000001"
    )
    # force offline
    alice.last_online = timezone.now() - timezone.timedelta(minutes=30)
    await alice.asave()
    event = await sync_to_async(GameEvent.objects.create)(
        guid="GUIDPOL000000000000000000000F", name="Police Test 2 [TT-140]", state=2
    )
    await sync_to_async(event.participants.create)(character=alice, rank=0)
    granted = await grant_race_wanted(object(), event)
    assert granted == []
    assert grant_mock.await_count == 0


@pytest.mark.asyncio
@patch("amc.handlers.tt_police.broadcast_server_message", new_callable=AsyncMock)
@patch("amc.handlers.tt_police.get_events", new_callable=AsyncMock)
async def test_alert_fires_when_still_racing(get_events_mock, send_mock, db):
    # 5-waypoint route, progress already at the final waypoint (idx 4):
    # every roll in [1..4] is satisfied -> announces.
    get_events_mock.return_value = [
        _racing_payload(5, section_index=4)
    ]
    event = await sync_to_async(GameEvent.objects.create)(
        guid="GUIDPOL000000000000000000000E", name="Alert Test [TT-350]", state=2
    )

    with patch.object(tt_police, "RACE_ALERT_CHECK_INTERVAL", 0):
        await announce_illegal_race(object(), event)
        await _flush_tasks()
    send_mock.assert_awaited_once()
    assert RACE_ALERT_MESSAGE == "An Illegal race is happening! Check Events!"


def _racing_payload(waypoints, section_index):
    return {
        "EventGuid": "GUIDPOL000000000000000000000E",
        "State": 2,
        "RaceSetup": {
            "Route": {"Waypoints": [{"Location": {"X": i}} for i in range(waypoints)]}
        },
        "Players": [
            {"PlayerName": "Racer", "SectionIndex": section_index}
        ],
    }


async def _flush_tasks():
    import asyncio

    await asyncio.sleep(0.05)
    me = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
    if pending:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True), timeout=10
        )


@pytest.mark.asyncio
@patch("amc.handlers.tt_police.broadcast_server_message", new_callable=AsyncMock)
@patch("amc.handlers.tt_police.get_events", new_callable=AsyncMock)
async def test_rearm_replaces_the_timer_single_announcement(
    get_events_mock, send_mock, db
):
    # Yuuka 2026-09-28: repeated starts stacked independent sleepers that
    # EACH announced. Re-arming must cancel the previous sleeper so only
    # ONE announcement lands, no matter how often the race is started.
    # 5 waypoints, final SectionIndex — any roll in [1..4] satisfied.
    get_events_mock.return_value = [
        _racing_payload(5, section_index=4)
    ]
    event = await sync_to_async(GameEvent.objects.create)(
        guid="GUIDPOL000000000000000000000E", name="Alert Re-arm [TT-270]", state=2
    )

    with patch.object(tt_police, "RACE_ALERT_CHECK_INTERVAL", 0):
        for _ in range(3):
            await announce_illegal_race(object(), event)
        await _flush_tasks()
    send_mock.assert_awaited_once()
    assert not tt_police._alert_tasks  # registry drained

    # Between-run reset: cancel kills the sleeper outright — no send.
    tt_police._announced_race_guids.clear()
    with patch.object(tt_police, "RACE_ALERT_CHECK_INTERVAL", 30):
        await announce_illegal_race(object(), event)
    tt_police.cancel_pending_race_alert(event.guid)
    await _flush_tasks()
    send_mock.assert_awaited_once()  # still the single original send
    assert not tt_police._alert_tasks


@pytest.mark.asyncio
@patch("amc.handlers.tt_police.broadcast_server_message", new_callable=AsyncMock)
@patch("amc.handlers.tt_police.get_events", new_callable=AsyncMock)
async def test_alert_silent_when_event_not_racing(get_events_mock, send_mock, db):
    # State 1 payload (no Route key at all) — the re-arm path.
    get_events_mock.return_value = [
        {"EventGuid": "GUIDPOL000000000000000000000E", "State": 1}
    ]
    event = await sync_to_async(GameEvent.objects.create)(
        guid="GUIDPOL000000000000000000000E", name="Alert Test 2 [TT-480]", state=1
    )

    with patch.object(tt_police, "RACE_ALERT_CHECK_INTERVAL", 0):
        await announce_illegal_race(object(), event)
        await _flush_tasks()
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
@patch("amc.handlers.tt_police.broadcast_server_message", new_callable=AsyncMock)
@patch("amc.handlers.tt_police.get_events", new_callable=AsyncMock)
async def test_alert_silent_below_rolled_fraction(
    get_events_mock, send_mock, db
):
    # Yuuka 2026-09-28: trigger is 50-100% of checkpoints passed. Pin the
    # roll at 100% (4 of 5 waypoints) and check that mid-route progress
    # stays silent.
    get_events_mock.return_value = [
        _racing_payload(5, section_index=2)
    ]
    event = await sync_to_async(GameEvent.objects.create)(
        guid="GUIDPOL000000000000000000000E", name="Alert Low [TT-350]", state=2
    )
    with patch.object(tt_police, "RACE_ALERT_CHECK_INTERVAL", 0), patch.object(
        tt_police, "RACE_ALERT_MIN_FRACTION", 0.9999
    ), patch.object(tt_police, "RACE_ALERT_MAX_FRACTION", 0.9999):
        await announce_illegal_race(object(), event)
        await _flush_tasks()
    send_mock.assert_not_awaited()
    # The rolled target persisted for the run (not re-rolled per sighting).
    assert event.guid in tt_police._alert_targets


@pytest.mark.asyncio
async def test_roll_target_bounds():
    for _ in range(200):
        t = await tt_police._roll_target(None, _racing_payload(45, section_index=0))
        assert t is not None and 1 <= t <= 44


@pytest.mark.asyncio
async def test_roll_target_no_waypoints():
    assert await tt_police._roll_target(None, {"State": 2}) is None
    assert await tt_police._roll_target(
        None, {"RaceSetup": {"Route": {"Waypoints": []}}}
    ) is None


@pytest.mark.asyncio
@patch("amc.criminals.create_or_refresh_wanted", new_callable=AsyncMock)
@patch("amc.handlers.tt_police.broadcast_server_message", new_callable=AsyncMock)
@patch("amc.handlers.tt_police.get_events", new_callable=AsyncMock)
async def test_rerun_reannounces_and_regrants(
    get_events_mock, send_mock, grant_mock, db
):
    # Yuuka 2026-10-02: the FIRST run of an event guid announced + granted
    # the star; every rerun of the same guid stayed silent until the worker
    # restarted — the announce marker was never cleared on finish and the
    # tick race pass only iterates state-2 rows, so nothing ever observed
    # the non-racing re-arm. Finish must reset the marker so each run
    # announces + grants fresh.
    get_events_mock.return_value = [_racing_payload(5, section_index=4)]
    event = await sync_to_async(GameEvent.objects.create)(
        guid="GUIDPOL000000000000000000000E",
        name="Alert Rerun [TT-350]",
        state=2,
    )
    char, _, _, _ = await _make_character(
        "RerunRacer", 7, "GUIDPOL0000000000000000000007"
    )
    char.last_online = timezone.now()
    await char.asave()
    await sync_to_async(event.participants.create)(character=char, rank=0)

    with patch.object(tt_police, "RACE_ALERT_CHECK_INTERVAL", 0):
        await announce_illegal_race(object(), event)
        await _flush_tasks()
    send_mock.assert_awaited_once()
    grant_mock.assert_awaited_once()

    # Run ends (the 2→1/3 transition hook calls this).
    tt_police.cancel_pending_race_alert(event.guid)
    assert event.guid not in tt_police._announced_race_guids

    # Rerun: fresh run row, same guid, racing again.
    rerun = await sync_to_async(GameEvent.objects.create)(
        guid="GUIDPOL000000000000000000000E",
        name="Alert Rerun [TT-350]",
        state=2,
    )
    await sync_to_async(rerun.participants.create)(character=char, rank=0)
    with patch.object(tt_police, "RACE_ALERT_CHECK_INTERVAL", 0):
        await announce_illegal_race(object(), rerun)
        await _flush_tasks()
    assert send_mock.await_count == 2
    assert grant_mock.await_count == 2


@pytest.mark.asyncio
@patch("amc.handlers.tt_police.broadcast_server_message", new_callable=AsyncMock)
@patch("amc.handlers.tt_police.get_events", new_callable=AsyncMock)
async def test_progress_gate_uses_db_section_stream(
    get_events_mock, send_mock, db
):
    # The live payload's per-player SectionIndex can stall at 0 while the
    # DB section stream keeps advancing (prod 2026-10-02: full 49/49 runs
    # never announced). The gate must read the DB progress too.
    get_events_mock.return_value = [_racing_payload(5, section_index=-1)]
    event = await sync_to_async(GameEvent.objects.create)(
        guid="GUIDPOL000000000000000000000E",
        name="Alert DB Gate [TT-350]",
        state=2,
    )
    char, _, _, _ = await _make_character(
        "GateRacer", 8, "GUIDPOL0000000000000000000008"
    )
    await sync_to_async(event.participants.create)(
        character=char, rank=0, section_index=4
    )

    with patch.object(tt_police, "RACE_ALERT_CHECK_INTERVAL", 0):
        await announce_illegal_race(object(), event)
        await _flush_tasks()
    send_mock.assert_awaited_once()


def _rot_config(route_name, laps):
    cfg = _race_config(route_name)
    cfg["NumLaps"] = laps
    return cfg


async def _make_setup(route_name, laps):
    config = _rot_config(route_name, laps)
    return await sync_to_async(RaceSetup.objects.create)(
        config=config,
        hash=RaceSetup.calculate_hash(config),
    )


@pytest.mark.asyncio
@patch("amc.events.announce", new_callable=AsyncMock)
async def test_rotation_posts_zero_lap_events_only(announce_mock, db):
    now = timezone.now()
    await sync_to_async(ScheduledEvent.objects.all().delete)()
    await sync_to_async(RaceSetup.objects.all().delete)()
    await sync_to_async(GameEvent.objects.all().delete)()

    sprint = await _make_setup("Sprint TT route", 0)
    circuit = await _make_setup("Circuit TT route", 3)
    for name, setup in (("Sprint SE", sprint), ("Circuit SE", circuit)):
        champ, _ = await sync_to_async(Championship.objects.get_or_create)(
            name=UNDERGROUND_CHAMPIONSHIP_NAME, defaults={"description": ""}
        )
        await sync_to_async(ScheduledEvent.objects.create)(
            name=name,
            race_setup=setup,
            time_trial=True,
            tt_class=None,  # class rolls per post now (Yuuka 2026-09-29)
            championship=champ,
            start_time=now - timedelta(hours=1),
            end_time=now + timedelta(hours=1),
        )
    mod = FakeModClient()
    await post_random_events({"http_client_mod": mod, "http_client": AsyncMock()})

    posted = [p["EventName"] for p in mod.posts]
    assert [
        re.sub(r"\s*(\(\d{3}\)\s*)?(\[TT-\d+\]|-\s*IR\s*-\s*\d+)$", "", n) for n in posted
    ] == ["Sprint SE"]
