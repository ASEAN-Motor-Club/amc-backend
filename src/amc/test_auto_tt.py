"""Tests for the revived auto-TT posting cron (post_random_events).

Key behaviors under test (2026-09-22 revival):
- the candidate pool only picks ScheduledEvents whose [start_time,
  end_time] window is live (expired windows must not be resurrected —
  _upsert_game_event links scheduled_event only inside the window);
- the in-game announce fires ONLY when at least one POST /events
  succeeded (the old version announced unconditionally — players saw
  "TT is up!" with no events actually created).
"""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from amc.events import post_random_events
from amc.models import (
    Character,
    GameEvent,
    GameEventCharacter,
    Player,
    RaceSetup,
    ScheduledEvent,
    TTClass,
)

# Auto-posted TT names carry the class tag: "Live TT [TT-480]".
import re as _re


def base_event_name(name):
    return _re.sub(r"\s*\[TT-\d+\]$", "", name or "")


def _race_config(route_name):
    return {
        "Route": {
            "RouteName": route_name,
            "Waypoints": [
                {
                    "Location": {"X": 1.0, "Y": 2.0, "Z": 3.0},
                    "Scale3D": {"X": 1.0, "Y": 12.0, "Z": 10.0},
                    "Rotation": {"X": 0.0, "Y": 0.0, "Z": 0.0, "W": 1.0},
                },
            ],
        },
        "NumLaps": 3,
        "VehicleKeys": [],
        "EngineKeys": [],
    }


class FakeResponse:
    def __init__(self, status, json_data=None):
        self.status = status
        self._json = json_data if json_data is not None else {}

    async def text(self):
        return "error body"

    async def json(self):
        return self._json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeModClient:
    """aiohttp client stand-in: POST status per EventName (fail_statuses),
    else 201; GET /events returns the queued live list."""

    def __init__(self, fail_statuses=None, live_events=None):
        self.fail_statuses = dict(fail_statuses or {})
        self.live_events = list(live_events or [])
        self.posts = []

    def post(self, path, json=None):
        self.posts.append(json)
        name = (json or {}).get("EventName")
        status = self.fail_statuses.get(base_event_name(name), 201)
        return FakeResponse(status)

    def get(self, path):
        if path == "/events":
            return FakeResponse(200, {"data": self.live_events})
        return FakeResponse(404)


async def _make_race(route_name):
    config = _race_config(route_name)
    return await sync_to_async(RaceSetup.objects.create)(
        config=config,
        hash=RaceSetup.calculate_hash(config),
    )


async def _clean_slate():
    """Defensive reset: this stack's async tests can leave rows behind, so
    every test starts from an empty ScheduledEvent/RaceSetup pool."""
    await sync_to_async(ScheduledEvent.objects.all().delete)()
    await sync_to_async(RaceSetup.objects.all().delete)()
    await sync_to_async(GameEvent.objects.all().delete)()


@pytest.mark.asyncio
@patch("amc.events.announce", new_callable=AsyncMock)
async def test_expired_window_events_not_posted(announce_mock, db):
    now = timezone.now()
    await _clean_slate()
    race = await _make_race("Expired TT route")
    await sync_to_async(ScheduledEvent.objects.create)(
        name="Expired TT",
        race_setup=race,
        time_trial=True,
        start_time=now - timedelta(days=30),
        end_time=now - timedelta(days=1),
    )
    mod = FakeModClient()
    await post_random_events({"http_client_mod": mod, "http_client": AsyncMock()})
    assert mod.posts == []
    announce_mock.assert_not_awaited()


@pytest.mark.asyncio
@patch("amc.events.announce", new_callable=AsyncMock)
async def test_active_window_event_posted_and_announced(announce_mock, db):
    now = timezone.now()
    await _clean_slate()
    race = await _make_race("Live TT route")
    await sync_to_async(ScheduledEvent.objects.create)(
        name="Live TT",
        race_setup=race,
        time_trial=True,
        start_time=now - timedelta(hours=1),
        end_time=now + timedelta(hours=1),
    )
    mod = FakeModClient()
    await post_random_events({"http_client_mod": mod, "http_client": AsyncMock()})

    posted = [p["EventName"] for p in mod.posts]
    assert [base_event_name(n) for n in posted] == ["Live TT"]
    # The random class tag rides in the posted event name.
    assert _re.search(r"\[TT-\d+\]$", posted[0])
    payload = mod.posts[0]
    assert payload["EventType"] == 1
    # Location → Translation rename on waypoints
    assert payload["RaceSetup"]["Route"]["Waypoints"][0] == {
        "Translation": {"X": 1.0, "Y": 2.0, "Z": 3.0},
        "Scale3D": {"X": 1.0, "Y": 12.0, "Z": 10.0},
        "Rotation": {"X": 0.0, "Y": 0.0, "Z": 0.0, "W": 1.0},
    }
    announce_mock.assert_awaited_once()
    assert "Live TT" in announce_mock.await_args.args[0]


@pytest.mark.asyncio
@patch("amc.events.announce", new_callable=AsyncMock)
async def test_all_posts_fail_no_announce(announce_mock, db):
    now = timezone.now()
    await _clean_slate()
    race = await _make_race("Doomed TT route")
    await sync_to_async(ScheduledEvent.objects.create)(
        name="Doomed TT",
        race_setup=race,
        time_trial=True,
        start_time=now - timedelta(hours=1),
        end_time=now + timedelta(hours=1),
    )
    mod = FakeModClient(fail_statuses={"Doomed TT": 400})
    await post_random_events({"http_client_mod": mod, "http_client": AsyncMock()})

    assert [p["EventName"] for p in mod.posts if p["EventName"]] == ["Doomed TT"]
    announce_mock.assert_not_awaited()


@pytest.mark.asyncio
@patch("amc.events.announce", new_callable=AsyncMock)
async def test_announce_lists_only_posted_events(announce_mock, db):
    """2 candidates, second POST fails → announce names only the first."""
    now = timezone.now()
    await _clean_slate()
    for name in ("Good TT", "Bad TT"):
        race = await _make_race(f"{name} route")
        await sync_to_async(ScheduledEvent.objects.create)(
            name=name,
            race_setup=race,
            time_trial=True,
            start_time=now - timedelta(hours=1),
            end_time=now + timedelta(hours=1),
        )
    mod = FakeModClient(fail_statuses={"Bad TT": 500})
    await post_random_events({"http_client_mod": mod, "http_client": AsyncMock()})

    announce_mock.assert_awaited_once()
    message = announce_mock.await_args.args[0]
    posted = [p["EventName"] for p in mod.posts if base_event_name(p["EventName"]) == "Good TT"]
    assert len(posted) == 1
    assert "Good TT" in message
    assert "Bad TT" not in message


async def _make_auto_event(guid, name="Auto TT", state=1):
    return await sync_to_async(GameEvent.objects.create)(
        guid=guid,
        name=name,
        state=state,
        auto_created=True,
    )


@pytest.mark.asyncio
@patch("amc.events.announce", new_callable=AsyncMock)
async def test_vanished_event_row_closed(announce_mock, db):
    """The game silently deletes unclaimed owner-less events — a Ready auto
    row whose guid is absent from the live list must be closed so its slot
    and setup free up again."""
    now = timezone.now()
    await _clean_slate()
    gone = await _make_auto_event("GUIDGONE00000000000000000000000")
    race = await _make_race("Live TT route")
    await sync_to_async(ScheduledEvent.objects.create)(
        name="Live TT",
        race_setup=race,
        time_trial=True,
        start_time=now - timedelta(hours=1),
        end_time=now + timedelta(hours=1),
    )
    mod = FakeModClient()  # live list empty → the event has vanished
    await post_random_events({"http_client_mod": mod, "http_client": AsyncMock()})

    await gone.arefresh_from_db()
    assert gone.state == 3
    # freed slot/setup → a replacement gets posted
    assert len(mod.posts) == 1


@pytest.mark.asyncio
@patch("amc.events.remove_event", new_callable=AsyncMock)
@patch("amc.events.announce", new_callable=AsyncMock)
async def test_unclaimed_live_event_rotated_out(announce_mock, remove_mock, db):
    """Each tick replaces owner-less Ready events nobody joined."""
    now = timezone.now()
    await _clean_slate()
    await _make_auto_event("GUIDLIVE000000000000000000000000")
    race = await _make_race("Live TT route")
    await sync_to_async(ScheduledEvent.objects.create)(
        name="Live TT",
        race_setup=race,
        time_trial=True,
        start_time=now - timedelta(hours=1),
        end_time=now + timedelta(hours=1),
    )
    mod = FakeModClient(live_events=[{"EventGuid": "GUIDLIVE000000000000000000000000"}])
    await post_random_events({"http_client_mod": mod, "http_client": AsyncMock()})

    remove_mock.assert_awaited_once()
    assert remove_mock.await_args.args[1] == "GUIDLIVE000000000000000000000000"
    row = await sync_to_async(GameEvent.objects.get)(
        guid="GUIDLIVE000000000000000000000000"
    )
    assert row.state == 3
    # freed slot → replacement posted + announced
    assert len(mod.posts) == 1
    announce_mock.assert_awaited_once()


@pytest.mark.asyncio
@patch("amc.events.remove_event", new_callable=AsyncMock)
@patch("amc.events.announce", new_callable=AsyncMock)
async def test_joined_event_not_rotated(announce_mock, remove_mock, db):
    """A player joined → the event is theirs; the cron must not touch it."""
    now = timezone.now()
    await _clean_slate()
    ge = await _make_auto_event("GUIDJOIN000000000000000000000000")
    race = await _make_race("Live TT route")
    await sync_to_async(ScheduledEvent.objects.create)(
        name="Live TT",
        race_setup=race,
        time_trial=True,
        start_time=now - timedelta(hours=1),
        end_time=now + timedelta(hours=1),
    )
    player = await sync_to_async(Player.objects.create)(unique_id=12345)
    character = await sync_to_async(Character.objects.create)(
        player=player, guid="AAAA0000", name="yuuka"
    )
    await sync_to_async(GameEventCharacter.objects.create)(
        game_event=ge, character=character, rank=0
    )
    mod = FakeModClient(live_events=[{"EventGuid": "GUIDJOIN000000000000000000000000"}])
    await post_random_events({"http_client_mod": mod, "http_client": AsyncMock()})

    remove_mock.assert_not_awaited()
    await ge.arefresh_from_db()
    assert ge.state == 1


# ---------------------------------------------------------------------------
# /events and /setup_event command behavior
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock, patch as sync_patch

from amc.command_framework import registry, CommandContext


def _make_ctx():
    ctx = MagicMock(spec=CommandContext)
    ctx.reply = AsyncMock()
    ctx.player = MagicMock()
    ctx.player.unique_id = 42
    ctx.character = MagicMock()
    ctx.character.guid = "CHARGUID0000000000000000000000"
    ctx.http_client_mod = MagicMock()
    ctx.timestamp = timezone.now()
    ctx.player_info = {"is_admin": True}
    return ctx


def _se(name, race, start, end):
    return sync_to_async(ScheduledEvent.objects.create)(
        name=name,
        race_setup=race,
        time_trial=True,
        start_time=start,
        end_time=end,
    )


@pytest.mark.asyncio
@sync_patch("amc.commands.events.setup_event", new_callable=AsyncMock)
async def test_setup_event_no_arg_starts_active_event(setup_mock, db):
    now = timezone.now()
    await _clean_slate()
    old_race = await _make_race("Expired TT route")
    await sync_to_async(ScheduledEvent.objects.create)(
        name="Expired TT",
        race_setup=old_race,
        time_trial=True,
        start_time=now - timedelta(days=2),
        end_time=now - timedelta(days=1),
    )
    race = await _make_race("Active TT route")
    active = await sync_to_async(ScheduledEvent.objects.create)(
        name="Active TT",
        race_setup=race,
        time_trial=True,
        start_time=now - timedelta(hours=1),
        end_time=now + timedelta(hours=1),
    )
    setup_mock.return_value = {"EventGuid": "G" * 32}

    executed = await registry.execute("/setup_event", _make_ctx())
    assert executed is True
    setup_mock.assert_awaited_once()
    assert setup_mock.await_args.args[2].pk == active.pk


@pytest.mark.asyncio
@sync_patch("amc.commands.events.setup_event", new_callable=AsyncMock)
async def test_setup_event_no_arg_no_active_replies_no_events(setup_mock, db):
    now = timezone.now()
    await _clean_slate()
    race = await _make_race("Expired TT route")
    await sync_to_async(ScheduledEvent.objects.create)(
        name="Expired TT",
        race_setup=race,
        time_trial=True,
        start_time=now - timedelta(days=30),
        end_time=now - timedelta(days=1),
    )
    ctx = _make_ctx()
    executed = await registry.execute("/setup_event", ctx)
    assert executed is True
    setup_mock.assert_not_awaited()
    ctx.reply.assert_awaited_once_with("No active events right now.")


@pytest.mark.asyncio
@sync_patch("amc.commands.events.setup_event", new_callable=AsyncMock)
async def test_events_lists_only_active(setup_mock, db):
    now = timezone.now()
    await _clean_slate()
    race = await _make_race("Active TT route")
    await sync_to_async(ScheduledEvent.objects.create)(
        name="Active TT",
        race_setup=race,
        time_trial=True,
        start_time=now - timedelta(hours=1),
        end_time=now + timedelta(hours=1),
    )
    await sync_to_async(ScheduledEvent.objects.create)(
        name="Future TT",
        race_setup=race,
        time_trial=True,
        start_time=now + timedelta(days=3),
        end_time=now + timedelta(days=4),
    )
    ctx = _make_ctx()
    await registry.execute("/events", ctx)
    message = ctx.reply.await_args.args[0]
    assert "Active TT" in message
    assert "Future TT" not in message


@pytest.mark.asyncio
async def test_events_empty_replies_no_events(db):
    await _clean_slate()
    ctx = _make_ctx()
    await registry.execute("/events", ctx)
    ctx.reply.assert_awaited_once_with("No active events right now.")
