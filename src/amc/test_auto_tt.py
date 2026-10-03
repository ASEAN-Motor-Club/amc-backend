"""Tests for the B2 underground rotation (post_random_events).

B2 (Yuuka 2026-10-03): the tick is NOT an auto-poster. It cycles SE
windows round-robin over the underground pool, pins the rolled HP class
onto the active SE, writes its requirements description, closes every
other underground window, and announces. /setup_event posts the decided
event; the original AMC Cup window system is the activeness mechanism.
"""

import re

# Classed event names carry the class tag: "Live TT - IR - 480".
import re as _re
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from amc.events import _rotation_reset, post_random_events
from amc.models import (
    Character,
    GameEvent,
    GameEventCharacter,
    Player,
    RaceSetup,
    ScheduledEvent,
)


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
        "NumLaps": 0,  # 0-lap only pool (Yuuka 2026-09-26 rotation rule)
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
    """aiohttp client stand-in: GET /events returns the queued live list;
    POST is recorded (the rotation must never POST)."""

    def __init__(self, live_events=None):
        self.live_events = list(live_events or [])
        self.posts = []

    def post(self, path, json=None):
        self.posts.append(json)
        return FakeResponse(201)

    def get(self, path):
        if path == "/events":
            return FakeResponse(200, {"data": self.live_events})
        return FakeResponse(404)


async def _make_race(route_name, num_laps=0):
    config = _race_config(route_name)
    config["NumLaps"] = num_laps
    return await sync_to_async(RaceSetup.objects.create)(
        config=config,
        hash=RaceSetup.calculate_hash(config),
    )


async def _get_class():
    """Pinned TT class for manual SEs."""
    from amc.models import TTClass

    cls, _ = await sync_to_async(TTClass.objects.get_or_create)(
        name="TT-480", defaults={"max_hp": 480}
    )
    return cls


async def _tt_class(name="TT-480", max_hp=480):
    from amc.models import TTClass

    return await sync_to_async(TTClass.objects.get_or_create)(
        name=name, defaults={"max_hp": max_hp}
    )


async def _clean_slate():
    """Defensive reset: this stack's async tests can leave rows behind, so
    every test starts from an empty ScheduledEvent/RaceSetup pool."""
    await sync_to_async(ScheduledEvent.objects.all().delete)()
    await sync_to_async(RaceSetup.objects.all().delete)()
    await sync_to_async(GameEvent.objects.all().delete)()


async def _ug_template(name, race, start, end, time_trial=True, is_rotation_instance=False):
    """Underground championship template (rotation pool, no pinned class)."""
    from amc.config import UNDERGROUND_CHAMPIONSHIP_NAME
    from amc.models import Championship

    champ, _ = await sync_to_async(Championship.objects.get_or_create)(
        name=UNDERGROUND_CHAMPIONSHIP_NAME, defaults={"description": ""}
    )
    return await sync_to_async(ScheduledEvent.objects.create)(
        name=name,
        race_setup=race,
        time_trial=time_trial,
        tt_class=None,
        championship=champ,
        start_time=start,
        end_time=end,
        is_rotation_instance=is_rotation_instance,
    )


@pytest.mark.asyncio
@patch("amc.events.announce", new_callable=AsyncMock)
async def test_rotation_never_posts_windows_one_se(announce_mock, db):
    """B2 core: the tick POSTs NOTHING; exactly one underground SE ends up
    windowed (class pinned + description written); the others are closed."""
    now = timezone.now()
    await _clean_slate()
    race_a = await _make_race("Route A")
    race_b = await _make_race("Route B")
    await _ug_template("Track A", race_a, now - timedelta(hours=1), now + timedelta(hours=1))
    await _ug_template("Track B", race_b, now - timedelta(hours=1), now + timedelta(hours=1))
    mod = FakeModClient()
    await post_random_events({"http_client_mod": mod, "http_client": AsyncMock()})

    assert mod.posts == []  # never auto-posts
    announce_mock.assert_awaited_once()
    rows = [r async for r in ScheduledEvent.objects.filter(end_time__gte=now, start_time__lte=now)]
    assert len(rows) == 1
    active = rows[0]
    assert active.tt_class_id is not None  # class pinned at rotation time
    assert active.description_in_game  # requirements text written
    expected = _rotation_reset(now)
    assert active.start_time == expected
    assert active.end_time == expected + timedelta(days=1)


@pytest.mark.asyncio
@patch("amc.events.announce", new_callable=AsyncMock)
async def test_rotation_round_robin_advances(announce_mock, db):
    """Two consecutive ticks -> the NEXT pool id holds the window (wrap at
    the end back to pool[0])."""
    now = timezone.now()
    await _clean_slate()
    names = ["Alpha", "Bravo", "Charlie"]
    for n in names:
        race = await _make_race(f"{n} route")
        await _ug_template(n, race, now - timedelta(hours=1), now + timedelta(hours=1))

    mod = FakeModClient()
    await post_random_events({"http_client_mod": mod, "http_client": AsyncMock()})
    first = await (
        ScheduledEvent.objects.filter(tt_class__isnull=False).afirst()
    )

    # Simulate the next window: the previous active SE is expired-windowed
    # by the tick itself (closed to window_start), so run the tick again
    # with time advanced past the current window start.
    await first.arefresh_from_db()
    # The tick closed every OTHER underground SE; the active one holds
    # [reset, reset+1d). Run a second tick "tomorrow": push the active SE's
    # window into the previous window by rewinding its start_time.
    await ScheduledEvent.objects.filter(pk=first.pk).aupdate(
        start_time=_rotation_reset(now) - timedelta(days=1),
        end_time=_rotation_reset(now) - timedelta(days=1) + timedelta(hours=1),
    )
    later = now + timedelta(days=2)
    with patch("amc.events.timezone") as tz_mock:
        tz_mock.now.return_value = later
        tz_mock.timedelta = None  # unused by the rotation directly
        await post_random_events({"http_client_mod": mod, "http_client": AsyncMock()})

    active = await (
        ScheduledEvent.objects.filter(
            start_time__gte=_rotation_reset(later),
            start_time__lt=_rotation_reset(later) + timedelta(days=1),
        ).afirst()
    )
    assert active is not None
    # pool order by id: Alpha < Bravo < Charlie; first tick picked Alpha,
    # second must pick Bravo.
    pool = [r async for r in ScheduledEvent.objects.filter(is_rotation_instance=False).order_by("id")]
    pnames = [r.name for r in pool]
    assert pnames[0] == "Alpha"  # sanity: pool order
    assert active.name == "Bravo"


@pytest.mark.asyncio
@patch("amc.events.announce", new_callable=AsyncMock)
async def test_rotation_closes_other_underground_windows_only(announce_mock, db):
    """Cup-era originals / non-underground SEs are NEVER touched by the
    rotation; only underground windows are closed."""
    now = timezone.now()
    await _clean_slate()
    from amc.models import Championship

    cup_champ, _ = await sync_to_async(Championship.objects.get_or_create)(
        name="AMC Cup Season 3", defaults={"description": ""}
    )
    race = await _make_race("Cup route")
    cup_se = await sync_to_async(ScheduledEvent.objects.create)(
        name="Cup Original",
        race_setup=race,
        time_trial=True,
        tt_class=await _get_class(),
        championship=cup_champ,
        start_time=now - timedelta(days=3),
        end_time=now + timedelta(days=4),
        description="Original cup description",
    )
    ug_race = await _make_race("UG route")
    await _ug_template("UG A", ug_race, now - timedelta(hours=1), now + timedelta(hours=1))
    await _ug_template("UG B", ug_race, now - timedelta(hours=1), now + timedelta(hours=1))

    mod = FakeModClient()
    await post_random_events({"http_client_mod": mod, "http_client": AsyncMock()})

    await cup_se.arefresh_from_db()
    assert cup_se.end_time == now + timedelta(days=4)  # untouched
    assert cup_se.tt_class_id is not None  # original class kept
    assert cup_se.description == "Original cup description"
    # Only ONE underground window open.
    from amc.config import UNDERGROUND_CHAMPIONSHIP_NAME

    open_ug = [
        r
        async for r in ScheduledEvent.objects.filter(
            championship__name=UNDERGROUND_CHAMPIONSHIP_NAME,
            end_time__gte=now,
            start_time__lte=now,
        )
    ]
    assert len(open_ug) == 1


@pytest.mark.asyncio
@patch("amc.events.announce", new_callable=AsyncMock)
async def test_rotation_announce_names_today_event(announce_mock, db):
    now = timezone.now()
    await _clean_slate()
    race = await _make_race("Announce route")
    await _ug_template("Announce TT", race, now - timedelta(hours=1), now + timedelta(hours=1))
    mod = FakeModClient()
    await post_random_events({"http_client_mod": mod, "http_client": AsyncMock()})
    message = announce_mock.await_args.args[0]
    assert "Announce TT - IR - " in message


# ---------------------------------------------------------------------------
# /setup_event on the decided SE
# ---------------------------------------------------------------------------

class _SetupClient:
    """Fake mod client for amc.events.setup_event: /events + /players GETs
    and POST /events capture (responses are async context managers)."""

    def __init__(self, player):
        self.player = player
        self.posts = []

    def post(self, path, json=None):
        self.posts.append(json)
        return FakeResponse(201, {})

    def get(self, path):
        if path == "/events":
            return FakeResponse(200, {"data": []})
        if path.startswith("/players/"):
            return FakeResponse(200, {"data": [self.player]})
        return FakeResponse(404)


@pytest.mark.asyncio
async def test_setup_event_pinned_class_ir_name_no_roll(db):
    """B2: /setup_event reads the SE's pinned class (decided at rotation
    time) and posts with the IR name tag. It NEVER rolls a class: a
    classless SE posts classless (no IR tag)."""
    from amc.events import setup_event

    now = timezone.now()
    await _clean_slate()
    cls, _ = await _tt_class("TT-350", max_hp=350)
    race = await _make_race("Pinned route")
    await _ug_template("Pinned TT", race, now - timedelta(hours=1), now + timedelta(hours=1))
    await ScheduledEvent.objects.filter(name="Pinned TT").aupdate(tt_class=cls)
    # Re-fetch with the FK loaded (async-context lazy FK access raises).
    se = await ScheduledEvent.objects.select_related("tt_class", "race_setup").aget(name="Pinned TT")
    client = _SetupClient({"CharacterGuid": "C" * 32})
    ok = await setup_event(now, 42, se, client)
    assert ok is True
    assert client.posts[0]["EventName"] == "Pinned TT - IR - 350"
    # No mirrors exist — there is no such thing as mirroring.
    assert not [r async for r in ScheduledEvent.objects.filter(is_rotation_instance=True)]

    # Classless SE -> classless post (exact SE name, no tag, no counter).
    race2 = await _make_race("Classless route")
    se2 = await _ug_template("Classless TT", race2, now - timedelta(hours=1), now + timedelta(hours=1))
    client2 = _SetupClient({"CharacterGuid": "C" * 32})
    await setup_event(now, 42, se2, client2)
    assert client2.posts[0]["EventName"] == "Classless TT"


@pytest.mark.asyncio
async def test_setup_event_classless_championship_stays_legal(db):
    """Non-underground classless SEs are never criminalized by
    /setup_event (Yuuka: SEs are reusable templates)."""
    from amc.events import setup_event
    from amc.models import Championship

    now = timezone.now()
    await _clean_slate()
    champ, _ = await sync_to_async(Championship.objects.get_or_create)(
        name="AMC Cup Season 3", defaults={"description": ""}
    )
    race = await _make_race("Legal template route")
    template = await sync_to_async(ScheduledEvent.objects.create)(
        name="Ara Grand Prix",
        race_setup=race,
        time_trial=False,
        tt_class=None,
        championship=champ,
        start_time=now - timedelta(hours=1),
        end_time=now + timedelta(hours=1),
    )
    client = _SetupClient({"CharacterGuid": "C" * 32})
    await setup_event(now, 42, template, client)
    assert "- IR -" not in client.posts[0]["EventName"]


@pytest.mark.asyncio
@patch("amc.events.announce", new_callable=AsyncMock)
async def test_ir_name_tag_parses_back_to_tt_class(announce_mock, db):
    """The IR name format ("… - IR - 140") is the class channel: the SSE
    upsert must parse it back into GameEvent.tt_class."""
    from amc.handlers.events import _parse_tt_class_tag
    cls, _ = await _tt_class("TT-140", max_hp=140)
    assert (await _parse_tt_class_tag("Unbeatable Record - Time Trial - IR - 140")) == cls
    assert (await _parse_tt_class_tag("Unbeatable Record - Time Trial - IR - 999")) is None
    assert (await _parse_tt_class_tag("Unbeatable Record - Time Trial")) is None


# ---------------------------------------------------------------------------
# /events and /setup_event command behavior
# ---------------------------------------------------------------------------

from amc.command_framework import CommandContext, registry  # noqa: E402
from unittest.mock import patch as sync_patch  # noqa: E402


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


def _se(name, race, start, end, tt_class=None, is_rotation_instance=False):
    return sync_to_async(ScheduledEvent.objects.create)(
        name=name,
        race_setup=race,
        time_trial=True,
        tt_class=tt_class,
        start_time=start,
        end_time=end,
        is_rotation_instance=is_rotation_instance,
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
        tt_class=await _get_class(),
        start_time=now - timedelta(days=2),
        end_time=now - timedelta(days=1),
    )
    race = await _make_race("Active TT route")
    active = await sync_to_async(ScheduledEvent.objects.create)(
        name="Active TT",
        race_setup=race,
        time_trial=True,
        tt_class=await _get_class(),
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
    await _ug_template("Expired TT", race, now - timedelta(days=30), now - timedelta(days=1))
    ctx = _make_ctx()
    executed = await registry.execute("/setup_event", ctx)
    assert executed is True
    setup_mock.assert_not_awaited()
    ctx.reply.assert_awaited_once_with("No active events right now.")


@pytest.mark.asyncio
@sync_patch("amc.commands.events.setup_event", new_callable=AsyncMock)
async def test_events_lists_every_windowed_se(setup_mock, db):
    """Original system behavior: /events lists EVERY windowed SE with its
    own description — no underground exclusion (the #311 exclusion is
    removed; the rotation's one-window rule keeps the list clean)."""
    now = timezone.now()
    await _clean_slate()
    race = await _make_race("Active TT route")
    await _se("Active TT", race, now - timedelta(hours=1), now + timedelta(hours=1))
    ug = await _ug_template(
        "Underground TT", race, now - timedelta(hours=1), now + timedelta(hours=1)
    )
    await ScheduledEvent.objects.filter(pk=ug.pk).aupdate(description_in_game="Underground street race — TT-350 requirements text.")
    await _se("Future TT", race, now + timedelta(days=3), now + timedelta(days=4))
    ctx = _make_ctx()
    await registry.execute("/events", ctx)
    message = ctx.reply.await_args.args[0]
    assert "Active TT" in message
    assert "Underground TT" in message  # listed — no more exclusion
    assert "Underground street race" in message  # its description shows
    assert "Future TT" not in message


@pytest.mark.asyncio
async def test_events_empty_replies_no_events(db):
    await _clean_slate()
    ctx = _make_ctx()
    await registry.execute("/events", ctx)
    ctx.reply.assert_awaited_once_with("No active events right now.")
