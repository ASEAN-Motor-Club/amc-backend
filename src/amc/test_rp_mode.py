"""Tests for the /rp_mode (/rp) command and the RP-mode vehicle-reset despawn."""

import asyncio
import time
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()


# ---------------------------------------------------------------------------
# /rp_mode (/rp) command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.commands.rp_rescue.refresh_player_name", new_callable=AsyncMock)
async def test_cmd_rp_mode_toggles_on(mock_refresh):
    """First invocation flips rp_mode from False to True, replies, refreshes name."""
    from amc.commands.rp_rescue import cmd_rp_mode
    from amc.command_framework import CommandContext
    from amc.factories import CharacterFactory, PlayerFactory

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player, rp_mode=False)

    http_client_mod = MagicMock()
    ctx = MagicMock(spec=CommandContext)
    ctx.character = character
    ctx.player = player
    ctx.http_client_mod = http_client_mod
    ctx.reply = AsyncMock()

    await cmd_rp_mode(ctx)
    # Let the fire-and-forget refresh_player_name task run.
    await asyncio.sleep(0)

    await character.arefresh_from_db()
    assert character.rp_mode is True

    mock_refresh.assert_awaited_once()
    # character passed positionally; http_client_mod is session
    assert mock_refresh.await_args.args[0].pk == character.pk
    assert mock_refresh.await_args.args[1] is http_client_mod

    ctx.reply.assert_awaited_once()
    reply_text = ctx.reply.await_args.args[0]
    assert "RP Mode Enabled" in str(reply_text)


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.commands.rp_rescue.refresh_player_name", new_callable=AsyncMock)
async def test_cmd_rp_mode_toggles_off(mock_refresh):
    """If rp_mode is already True, invocation turns it off."""
    from amc.commands.rp_rescue import cmd_rp_mode
    from amc.command_framework import CommandContext
    from amc.factories import CharacterFactory, PlayerFactory

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player, rp_mode=True)

    ctx = MagicMock(spec=CommandContext)
    ctx.character = character
    ctx.player = player
    ctx.http_client_mod = MagicMock()
    ctx.reply = AsyncMock()

    await cmd_rp_mode(ctx)
    await asyncio.sleep(0)

    await character.arefresh_from_db()
    assert character.rp_mode is False

    mock_refresh.assert_awaited_once()
    reply_text = ctx.reply.await_args.args[0]
    assert "RP Mode Disabled" in str(reply_text)


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_cmd_rp_alias_resolves_to_same_handler():
    """The /rp alias is registered on the same handler as /rp_mode."""
    from amc.command_framework import registry
    # Ensure the command module is imported so the decorator has fired.
    import amc.commands.rp_rescue  # noqa: F401

    cmd_entries = [
        c for c in registry.commands if "/rp_mode" in c["aliases"]
    ]
    assert len(cmd_entries) == 1
    entry = cmd_entries[0]
    assert "/rp" in entry["aliases"]
    assert entry["func"].__name__ == "cmd_rp_mode"


# ---------------------------------------------------------------------------
# ServerResetVehicleAt — despawn enforcement
# ---------------------------------------------------------------------------


def _reset_vehicle_event(character_guid):
    return {
        "hook": "ServerResetVehicleAt",
        "timestamp": int(time.time()),
        "data": {"CharacterGuid": str(character_guid)},
    }


@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock, return_value=100_000)
@patch("amc.webhook.announce", new_callable=AsyncMock)
class VehicleResetDespawnTests(TestCase):
    def setUp(self):
        cache.clear()

    async def _setup_character(self, *, rp_mode: bool):
        from amc.factories import CharacterFactory, PlayerFactory
        from amc.models import PlayerStatusLog

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            guid="reset-rp-guid",
            rp_mode=rp_mode,
        )
        await PlayerStatusLog.objects.acreate(
            character=character,
            timespan=(
                timezone.now() - timedelta(minutes=5),
                timezone.now() - timedelta(minutes=4),
            ),
        )
        return character

    async def test_despawns_when_rp_mode_on(
        self, mock_announce, mock_treasury
    ):
        from amc.webhook import process_events

        character = await self._setup_character(rp_mode=True)

        await process_events(
            [_reset_vehicle_event(character.guid)],
            http_client=MagicMock(),
            http_client_mod=MagicMock(),
        )

        await asyncio.sleep(0)

        mock_announce.assert_not_called()

    async def test_noop_when_rp_mode_off(
        self, mock_announce, mock_treasury
    ):
        from amc.webhook import process_events

        character = await self._setup_character(rp_mode=False)

        await process_events(
            [_reset_vehicle_event(character.guid)],
            http_client=MagicMock(),
            http_client_mod=MagicMock(),
        )

        await asyncio.sleep(0)

        mock_announce.assert_not_called()
