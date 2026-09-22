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
from amc.models import RaceSetup, ScheduledEvent


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
    def __init__(self, status):
        self.status = status

    async def text(self):
        return "error body"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeModClient:
    """aiohttp client stand-in: POST status per EventName (fail_statuses),
    else 201."""

    def __init__(self, fail_statuses=None):
        self.fail_statuses = dict(fail_statuses or {})
        self.posts = []

    def post(self, path, json=None):
        self.posts.append(json)
        name = (json or {}).get("EventName")
        status = self.fail_statuses.get(name, 201)
        return FakeResponse(status)


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

    assert [p["EventName"] for p in mod.posts] == ["Live TT"]
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
    posted = [p["EventName"] for p in mod.posts if p["EventName"] == "Good TT"]
    assert posted == ["Good TT"]
    assert "Good TT" in message
    assert "Bad TT" not in message
