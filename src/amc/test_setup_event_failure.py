"""Regression tests for /setup_event when the mod rejects the create POST."""

from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from amc.events import setup_event

PLAYER_ID = "76561198378447512"
CHAR_GUID = "E603C74946EFF3F8834C9AAB3D0E3181"


class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def text(self):
        return str(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Client:
    def __init__(self, post_status=201, post_payload=None, post_exc=None):
        self.post_status = post_status
        self.post_payload = post_payload or {}
        self.post_exc = post_exc
        self.posted = None

    def get(self, url):
        if url == "/events":
            return _Resp(200, {"data": []})
        return _Resp(
            200,
            {"data": [{"CharacterGuid": CHAR_GUID, "UniqueNetId": PLAYER_ID}]},
        )

    def post(self, url, json=None):
        assert url == "/events"
        self.posted = json
        if self.post_exc:
            raise self.post_exc
        return _Resp(self.post_status, self.post_payload)


def _scheduled_event():
    se = MagicMock()
    se.name = "AMC Test Race"
    se.event_type = 1
    se.tt_class_id = None
    se.race_setup.config = {
        "NumLaps": 2,
        "Route": {
            "RouteName": "Test Route",
            "Waypoints": [
                {
                    "Location": {"X": 1.0, "Y": 2.0, "Z": 3.0},
                    "Rotation": {"X": 0.0, "Y": 0.0, "Z": 0.0, "W": 1.0},
                    "Scale3D": {"X": 1.0, "Y": 1.0, "Z": 10.0},
                }
            ],
        },
        "VehicleKeys": [],
        "EngineKeys": [],
    }
    return se


@contextmanager
def _mocks():
    """Patch the sync ORM calls so these tests need no DB rows."""
    with ExitStack() as stack:
        legality = stack.enter_context(
            patch("amc.handlers.events._race_legality_for", return_value="legal")
        )
        char_filter = stack.enter_context(patch("amc.events.Character.objects.filter"))
        create_row = stack.enter_context(
            patch("amc.events.GameEvent.objects.aget_or_create", new_callable=AsyncMock)
        )
        event_filter = stack.enter_context(patch("amc.events.GameEvent.objects.filter"))
        yield legality, char_filter, create_row, event_filter


@pytest.mark.asyncio
async def test_mod_500_drops_precreated_row_and_hides_lua_traceback():
    client = _Client(
        post_status=500,
        post_payload={"error": "EventManager.lua:177: TArray index out of range"},
    )
    se = _scheduled_event()

    with _mocks() as mocks:
        legality, char_filter, create_row, event_filter = mocks
        char_filter.return_value.afirst = AsyncMock(return_value=MagicMock())
        event_filter.return_value.adelete = AsyncMock()

        with pytest.raises(Exception) as exc:
            await setup_event(MagicMock(), PLAYER_ID, se, client)

        message = str(exc.value)
        assert "could not create the event" in message
        assert "TArray" not in message
        assert "EventManager.lua" not in message

        event_filter.assert_called_once_with(guid=client.posted["EventGuid"])
        event_filter.return_value.adelete.assert_awaited_once()


@pytest.mark.asyncio
async def test_mod_success_keeps_precreated_row():
    client = _Client(post_status=201)
    se = _scheduled_event()

    with _mocks() as mocks:
        legality, char_filter, create_row, event_filter = mocks
        char_filter.return_value.afirst = AsyncMock(return_value=MagicMock())
        event_filter.return_value.adelete = AsyncMock()

        assert await setup_event(MagicMock(), PLAYER_ID, se, client) is True
        event_filter.assert_not_called()
        event_filter.return_value.adelete.assert_not_awaited()


@pytest.mark.asyncio
async def test_transport_error_drops_precreated_row():
    client = _Client(post_exc=RuntimeError("connection reset"))
    se = _scheduled_event()

    with _mocks() as mocks:
        legality, char_filter, create_row, event_filter = mocks
        char_filter.return_value.afirst = AsyncMock(return_value=MagicMock())
        event_filter.return_value.adelete = AsyncMock()

        with pytest.raises(Exception) as exc:
            await setup_event(MagicMock(), PLAYER_ID, se, client)

        assert "could not create the event" in str(exc.value)
        event_filter.return_value.adelete.assert_awaited_once()
