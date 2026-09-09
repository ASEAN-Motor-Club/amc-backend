"""Callback-level tests for the /check_all_players cog (amc_cogs/parts_audit.py)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from amc_cogs.parts_audit import PartsAuditCog

pytestmark = [pytest.mark.asyncio]


def _make_interaction():
    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.user.display_name = "admin"
    return interaction


def _make_cog():
    cog = PartsAuditCog(MagicMock())
    cog.bot.http_client_game = object()
    cog.bot.http_client_mod = object()
    return cog


async def test_check_all_players_posts_and_confirms():
    cog = _make_cog()
    interaction = _make_interaction()

    async def fake_players(session):
        return [
            ("1", {"name": "alpha", "character_guid": "guid-1"}),
            ("2", {"name": "beta", "character_guid": None}),  # no character — skipped
        ]

    audit = AsyncMock(return_value=MagicMock())
    with (
        patch("amc_cogs.parts_audit.get_players", fake_players),
        patch("amc_cogs.parts_audit.audit_character", audit),
    ):
        await PartsAuditCog.check_all_players.callback(cog, interaction)

    interaction.response.defer.assert_awaited_once()
    audit.assert_awaited_once()
    assert audit.await_args.args[1] == "guid-1"
    assert audit.await_args.args[2] == "alpha"
    interaction.followup.send.assert_awaited_once()
    text = interaction.followup.send.await_args.args[0]
    assert "1 player(s)" in text
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True


async def test_check_all_players_empty_server():
    cog = _make_cog()
    interaction = _make_interaction()

    async def fake_players(session):
        return []

    with patch("amc_cogs.parts_audit.get_players", fake_players):
        await PartsAuditCog.check_all_players.callback(cog, interaction)

    interaction.followup.send.assert_awaited_once_with("No players online.", ephemeral=True)


async def test_check_all_players_fetch_failure():
    cog = _make_cog()
    interaction = _make_interaction()

    async def boom(session):
        raise RuntimeError("game api down")

    with patch("amc_cogs.parts_audit.get_players", boom):
        await PartsAuditCog.check_all_players.callback(cog, interaction)

    interaction.followup.send.assert_awaited_once()
    assert "Failed to fetch" in interaction.followup.send.await_args.args[0]
