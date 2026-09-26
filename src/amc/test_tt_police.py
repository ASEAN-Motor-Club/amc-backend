"""Tests for the illegal-race police trigger (handlers/tt_police.py) and
the 0-lap-only rotation filter (events.post_random_events).

Covers the contract agreed for the Yuuka 2026-09-26 request:

* every participant gets the wanted star at race start (create_or_refresh_
  wanted, system-trigger path) except players DQ'd at the line
* a missing Character row skips that player without breaking the rest
* the 60s announcement fires only while the event is still live AND
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
    RACE_ALERT_DELAY_SECONDS,
    RACE_ALERT_MESSAGE,
    announce_illegal_race,
    mark_racers_wanted,
)
from amc.models import Character, GameEvent, RaceSetup, ScheduledEvent
from amc.test_auto_tt import (  # noqa: F401  (fixtures shared)
    FakeModClient,
    _race_config,
)

pytestmark = pytest.mark.django_db


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
@patch("amc.handlers.tt_police.create_or_refresh_wanted", new_callable=AsyncMock)
async def test_all_racers_marked_except_dqd(wanted_mock, db):
    wanted_mock.return_value = (None, True)
    await _make_character("Alice", 1, "GUIDPOL000000000000000000000001")
    await _make_character("Bob", 2, "GUIDPOL000000000000000000000002")
    event = await sync_to_async(GameEvent.objects.create)(
        guid="GUIDPOL0000000000000000000000E", name="Police Test [TT-270]", state=2
    )
    marked = await mark_racers_wanted(
        object(),
        event,
        {"Players": [
            _player("GUIDPOL000000000000000000000001", "1", "Alice"),
            _player("GUIDPOL000000000000000000000002", "2", "Bob"),
            _player("GUIDPOL000000000000000000000003", "3", "DQdDan"),
        ]},
        disqualified=["DQdDan"],
    )
    assert marked == ["Alice", "Bob"]
    assert wanted_mock.await_count == 2
    marked_guids = {c.args[0].guid for c in wanted_mock.await_args_list}
    assert marked_guids == {
        "GUIDPOL000000000000000000000001",
        "GUIDPOL000000000000000000000002",
    }


@pytest.mark.asyncio
@patch("amc.handlers.tt_police.create_or_refresh_wanted", new_callable=AsyncMock)
async def test_unknown_character_skipped_others_marked(wanted_mock, db):
    wanted_mock.return_value = (None, True)
    await _make_character("Alice", 1, "GUIDPOL000000000000000000000001")
    event = await sync_to_async(GameEvent.objects.create)(
        guid="GUIDPOL000000000000000000000F", name="Police Test 2 [TT-140]", state=2
    )
    marked = await mark_racers_wanted(
        object(),
        event,
        {"Players": [
            _player("GUIDPOL000000000000000000000001", "1", "Alice"),
            _player("GUIDPOLGHOST00000000000000000001", "9", "Ghost"),
        ]},
        disqualified=[],
    )
    assert marked == ["Alice"]
    assert wanted_mock.await_count == 1


@pytest.mark.asyncio
@patch("amc.handlers.tt_police.send_system_message", new_callable=AsyncMock)
@patch("amc.handlers.tt_police.get_events", new_callable=AsyncMock)
async def test_alert_fires_when_still_racing(get_events_mock, send_mock, db):
    get_events_mock.return_value = {
        "data": [{"EventGuid": "GUIDPOL000000000000000000000E", "State": 2}]
    }
    event = await sync_to_async(GameEvent.objects.create)(
        guid="GUIDPOL000000000000000000000E", name="Alert Test [TT-350]", state=2
    )

    with patch.object(tt_police, "RACE_ALERT_DELAY_SECONDS", 0):
        await announce_illegal_race(object(), event)
        await _flush_tasks()
    send_mock.assert_awaited_once()
    assert RACE_ALERT_DELAY_SECONDS == 60  # production value untouched
    assert RACE_ALERT_MESSAGE == "An Illegal race is happening! Check Events!"


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
@patch("amc.handlers.tt_police.send_system_message", new_callable=AsyncMock)
@patch("amc.handlers.tt_police.get_events", new_callable=AsyncMock)
async def test_alert_silent_when_event_not_racing(get_events_mock, send_mock, db):
    get_events_mock.return_value = {
        "data": [{"EventGuid": "GUIDPOL000000000000000000000E", "State": 1}]
    }
    event = await sync_to_async(GameEvent.objects.create)(
        guid="GUIDPOL000000000000000000000E", name="Alert Test 2 [TT-480]", state=1
    )

    with patch.object(tt_police, "RACE_ALERT_DELAY_SECONDS", 0):
        await announce_illegal_race(object(), event)
        await _flush_tasks()
    send_mock.assert_not_awaited()


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
        await sync_to_async(ScheduledEvent.objects.create)(
            name=name,
            race_setup=setup,
            time_trial=True,
            start_time=now - timedelta(hours=1),
            end_time=now + timedelta(hours=1),
        )
    mod = FakeModClient()
    await post_random_events({"http_client_mod": mod, "http_client": AsyncMock()})

    posted = [p["EventName"] for p in mod.posts]
    assert [re.sub(r" \[TT-\d+\]$", "", n) for n in posted] == ["Sprint SE"]
