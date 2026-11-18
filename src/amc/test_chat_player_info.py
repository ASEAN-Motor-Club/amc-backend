"""Host Web API password plumbing + chat player_info fallback.

Motor Town 0.7.20 made the Host Web API fail-closed: while the server's
``HostWebAPIServerPassword`` is empty, every request (including
``/player/list``) answers ``{"succeeded": false, "message": "Invalid
password"}``.  ``get_player_info`` then returns ``None``, the chat handler used
to hand the command framework an empty ``player_info``, and ``/tp2marker``
replied "Could not determine your position — try again.".

Two behaviours are pinned here:
  1. ``game_api_request`` sends the configured password by default.
  2. The chat handler falls back to the mod server's ``/players`` payload when
     the native payload carries no ``Location``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async

from amc.factories import CharacterFactory, PlayerFactory
from amc.game_server import game_api_request
from amc.handlers.chat import handle_server_send_chat
from amc.webhook_context import EventContext


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeSession:
    def __init__(self, payload=None):
        self.payload = payload or {"succeeded": True, "data": {}}
        self.urls: list[str] = []

    def get(self, url, **_kwargs):
        self.urls.append(str(url))
        return _FakeResponse(self.payload)


@pytest.mark.asyncio
async def test_game_api_request_sends_configured_password():
    """The configured password is sent when a caller omits one."""
    from django.test import override_settings

    session = _FakeSession()
    with override_settings(GAME_SERVER_API_PASSWORD="hackme"):
        await game_api_request(session, "http://127.0.0.1:8080/player/list")

    assert "password=hackme" in session.urls[0]


@pytest.mark.asyncio
async def test_game_api_request_explicit_password_wins():
    """An explicit password argument is not overridden by the setting."""
    from django.test import override_settings

    session = _FakeSession()
    with override_settings(GAME_SERVER_API_PASSWORD="hackme"):
        await game_api_request(
            session, "http://127.0.0.1:8080/player/list", password="other"
        )

    assert "password=other" in session.urls[0]


async def _character():
    player = await sync_to_async(PlayerFactory)()
    return await sync_to_async(CharacterFactory)(player=player), player


def _chat_event(character, player):
    return {
        "data": {
            "Message": "/tp2marker",
            "Category": 2,
            "CharacterGuid": str(character.guid),
            "UniqueID": str(player.unique_id),
        }
    }


async def _run_handler(native_info, mod_info):
    character, player = await _character()
    ctx = EventContext(http_client=object(), http_client_mod=object())

    with (
        patch(
            "amc.handlers.chat.get_player_info",
            new=AsyncMock(return_value=native_info),
        ),
        patch(
            "amc.handlers.chat.get_mod_player",
            new=AsyncMock(return_value=mod_info),
        ) as mock_mod,
        patch(
            "amc.command_framework.registry.execute",
            new=AsyncMock(),
        ) as mock_execute,
        patch("amc.api.bot_events.emit_bot_event", new=AsyncMock()),
    ):
        await handle_server_send_chat(
            _chat_event(character, player), player, character, ctx
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    return mock_mod, mock_execute


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_chat_handler_falls_back_to_mod_player_info():
    """A dead native API must not blank out ctx.player_info."""
    location = {"X": 1.0, "Y": 2.0, "Z": 3.0}
    mock_mod, mock_execute = await _run_handler(
        None,
        {
            "Location": location,
            "VehicleKey": "Zero",
            "bIsAdmin": True,
            "PlayerName": "Tester",
        },
    )

    mock_mod.assert_awaited_once()
    cmd_ctx = mock_execute.call_args.args[1]
    assert cmd_ctx.player_info["Location"] == location
    assert cmd_ctx.player_info["VehicleKey"] == "Zero"
    assert cmd_ctx.player_info["bIsAdmin"] is True


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_chat_handler_keeps_native_player_info():
    """When the native API answers with a Location, the mod is not consulted."""
    native = {
        "Location": {"X": 9.0, "Y": 8.0, "Z": 7.0},
        "VehicleKey": "None",
        "bIsAdmin": False,
        "PlayerName": "Still Fine",
    }
    mock_mod, mock_execute = await _run_handler(native, None)

    mock_mod.assert_not_awaited()
    cmd_ctx = mock_execute.call_args.args[1]
    assert cmd_ctx.player_info["Location"] == native["Location"]
