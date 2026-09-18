"""Tests for the /drivers_license cog wiring."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from amc_cogs.license import DriversLicenseCog, _fetch_avatar_bytes


def _make_cog():
    bot = MagicMock()
    return DriversLicenseCog(bot), bot


def _make_interaction(user_id=1155069673512120341):
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.user.display_name = "Tester"
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def test_command_registered():
    cog, _ = _make_cog()
    cmd = cog.drivers_license
    assert cmd.name == "drivers_license"
    assert "card" in (cmd.description or "").lower()


def test_unlinked_player_gets_verify_hint():
    cog, _ = _make_cog()
    interaction = _make_interaction()

    async def scenario():
        class FakeDoesNotExist(Exception):
            pass

        with patch("amc_cogs.license.Player") as player_stub:
            player_stub.DoesNotExist = FakeDoesNotExist
            player_stub.objects.aget = AsyncMock(side_effect=FakeDoesNotExist())
            await DriversLicenseCog.drivers_license.callback(cog, interaction)

    asyncio.run(scenario())
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    call = interaction.followup.send.await_args
    kwargs = call.kwargs
    content = call.args[0] if call.args else kwargs.get("content", "")
    assert "/verify" in content
    assert kwargs.get("ephemeral") is True
    assert "file" not in kwargs


def test_linked_player_gets_card_file():
    cog, _bot = _make_cog()
    interaction = _make_interaction()

    player = MagicMock()
    player.discord_name = "Meehoi San"

    fake_png = b"\x89PNG\r\n\x1a\n" + b"0" * 64

    async def scenario():
        with (
            patch("amc_cogs.license.Player") as player_stub,
            patch("amc_cogs.license.PlayerStatusLog") as log_stub,
            patch("amc_cogs.license.render_license_card", return_value=fake_png) as render,
            patch("amc_cogs.license._fetch_avatar_bytes", new=AsyncMock(return_value=None)),
        ):
            player_stub.objects.aget = AsyncMock(return_value=player)
            recent = datetime.now(UTC) - timedelta(hours=2)
            agg = {"first": None, "latest": recent}
            log_stub.objects.filter.return_value.aaggregate = AsyncMock(
                return_value=agg
            )
            await DriversLicenseCog.drivers_license.callback(cog, interaction)
        return render

    render = asyncio.run(scenario())
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    kwargs = interaction.followup.send.await_args.kwargs
    assert "file" in kwargs
    assert kwargs.get("ephemeral") is True
    file = kwargs["file"]
    assert isinstance(file, discord.File)
    assert file.filename == "amc_license.png"
    # render ran with the caller's identity, not a name-based lookup
    assert render.call_args.kwargs["discord_id"] == "1155069673512120341"
    assert render.call_args.kwargs["joined"] is None


def test_stale_player_gets_recency_gate_message():
    cog, _bot = _make_cog()
    interaction = _make_interaction()

    player = MagicMock()
    player.discord_name = "Meehoi San"

    async def scenario():
        with (
            patch("amc_cogs.license.Player") as player_stub,
            patch("amc_cogs.license.PlayerStatusLog") as log_stub,
            patch(
                "amc_cogs.license.render_license_card"
            ) as render,  # must never run
        ):
            player_stub.objects.aget = AsyncMock(return_value=player)
            stale = datetime.now(UTC) - timedelta(days=8)
            agg = {"first": stale, "latest": stale}
            log_stub.objects.filter.return_value.aaggregate = AsyncMock(
                return_value=agg
            )
            await DriversLicenseCog.drivers_license.callback(cog, interaction)
        return render

    render = asyncio.run(scenario())
    render.assert_not_called()
    call = interaction.followup.send.await_args
    content = call.args[0] if call.args else call.kwargs.get("content", "")
    assert "7 days" in content
    assert call.kwargs.get("ephemeral") is True
    assert "file" not in call.kwargs


def test_no_login_rows_fails_recency_gate():
    cog, _bot = _make_cog()
    interaction = _make_interaction()

    player = MagicMock()
    player.discord_name = "Meehoi San"

    async def scenario():
        with (
            patch("amc_cogs.license.Player") as player_stub,
            patch("amc_cogs.license.PlayerStatusLog") as log_stub,
            patch(
                "amc_cogs.license.render_license_card"
            ) as render,
        ):
            player_stub.objects.aget = AsyncMock(return_value=player)
            agg = {"first": None, "latest": None}
            log_stub.objects.filter.return_value.aaggregate = AsyncMock(
                return_value=agg
            )
            await DriversLicenseCog.drivers_license.callback(cog, interaction)
        return render

    render = asyncio.run(scenario())
    render.assert_not_called()
    assert interaction.followup.send.await_args.kwargs.get("ephemeral") is True


def test_fetch_avatar_bytes_cache_hit_avoids_refetch():
    cog, bot = _make_cog()
    data = b"avatar-bytes"
    cog._avatar_cache[42] = (time.monotonic(), data)  # fresh entry

    async def scenario():
        # ensure get_user is never called on a cache hit
        return await _fetch_avatar_bytes(bot, 42, cog._avatar_cache)

    got = asyncio.run(scenario())
    assert got == data
    bot.get_user.assert_not_called()


def test_fetch_avatar_bytes_failure_returns_none():
    cog, bot = _make_cog()
    bot.get_user.return_value = None
    bot.fetch_user = AsyncMock(side_effect=discord.NotFound(MagicMock(), "x"))

    async def scenario():
        return await _fetch_avatar_bytes(bot, 4242, cog._avatar_cache)

    got = asyncio.run(scenario())
    assert got is None
