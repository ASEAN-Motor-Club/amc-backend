"""Tests for the ActiveRoleCog (/active_sync admin command)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from django.test import override_settings

from amc_cogs.active_role import ActiveRoleCog


def _interaction(*, admin: bool, guild: bool = True):
    interaction = MagicMock()
    interaction.response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock())
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    if guild:
        guild_obj = MagicMock()
        admin_role = MagicMock()
        guild_obj.get_role.return_value = admin_role
        interaction.guild = guild_obj
        # spec=discord.Member so the isinstance(member) guard passes
        interaction.user = MagicMock(spec=discord.Member)
        interaction.user.roles = [admin_role] if admin else []
    else:
        interaction.guild = None
        interaction.user = MagicMock(spec=[])  # bare User: no .roles
    return interaction


def test_cog_instantiates_and_registers_command():
    cog = ActiveRoleCog(MagicMock())
    assert any(cmd.name == "active_sync" for cmd in cog.get_app_commands())


@override_settings(DISCORD_ADMIN_ROLE_ID=1395460420189421713)
def test_active_sync_denies_non_admin():
    cog = ActiveRoleCog(MagicMock())
    interaction = _interaction(admin=False)
    asyncio.run(ActiveRoleCog.active_sync.callback(cog, interaction))
    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs.get("ephemeral") is True


@override_settings(DISCORD_ADMIN_ROLE_ID=1395460420189421713)
def test_active_sync_reports_summary_followup():
    cog = ActiveRoleCog(MagicMock())
    interaction = _interaction(admin=True)
    summary = {"skipped": False, "added": 3, "removed": 1, "missing": 0}
    with patch(
        "amc_cogs.active_role.sync_active_role", new=AsyncMock(return_value=summary)
    ):
        asyncio.run(ActiveRoleCog.active_sync.callback(cog, interaction))
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once()
    text = interaction.followup.send.await_args.args[0]
    assert "added **3**" in text and "removed **1**" in text


@override_settings(DISCORD_ADMIN_ROLE_ID=1395460420189421713)
def test_active_sync_reports_skipped_configuration():
    cog = ActiveRoleCog(MagicMock())
    interaction = _interaction(admin=True)
    summary = {"skipped": True, "added": 0, "removed": 0, "missing": 0}
    with patch(
        "amc_cogs.active_role.sync_active_role", new=AsyncMock(return_value=summary)
    ):
        asyncio.run(ActiveRoleCog.active_sync.callback(cog, interaction))
    text = interaction.followup.send.await_args.args[0]
    assert "skipped" in text


@pytest.mark.django_db
def test_registration_in_discord_client_module():
    """The cog must be importable and listed for registration in setup_hook."""
    import inspect

    import amc.discord_client as dc

    source = inspect.getsource(dc)
    assert "ActiveRoleCog" in source
