"""/check_all_players — silent parts audit over every online player.

Posts one embed per player (power/engine/intake/turbo/tires summary, same
lines as the event-join audit) to ``DISCORD_PARTS_LOG_CHANNEL_ID`` and
replies to the invoking admin with a short confirmation.  Admin-gated
(``DISCORD_ADMIN_ROLE_ID``) — it fans out one mod fetch per online player.
"""

import logging

import discord
from django.conf import settings
from discord import app_commands
from discord.ext import commands

from amc.game_server import get_players
from amc.parts_audit import audit_character

logger = logging.getLogger(__name__)


class PartsAuditCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="check_all_players",
        description="Run a silent parts check on every online player (posts to the parts-log channel)",
    )
    @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
    async def check_all_players(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            players = await get_players(self.bot.http_client_game)
        except Exception:
            logger.warning("check_all_players: player list fetch failed", exc_info=True)
            await interaction.followup.send(
                "Failed to fetch the online player list.", ephemeral=True
            )
            return

        if not players:
            await interaction.followup.send("No players online.", ephemeral=True)
            return

        checked = 0
        posted = 0
        for _pid, p_data in players:
            character_guid = p_data.get("character_guid")
            if not character_guid:
                continue
            player_name = p_data.get("name") or f"uid:{_pid}"
            result = await audit_character(
                self.bot.http_client_mod,
                character_guid,
                player_name,
                discord_client=self.bot,
                source=f"manual check by {interaction.user.display_name}",
            )
            checked += 1
            if result is not None:
                posted += 1

        channel_note = "posted to <#%s>" % settings.DISCORD_PARTS_LOG_CHANNEL_ID if settings.DISCORD_PARTS_LOG_CHANNEL_ID else "log channel not configured — summaries in worker journal only"
        await interaction.followup.send(
            f"Checked {checked} player(s) with an active vehicle, {posted} report(s) {channel_note}.",
            ephemeral=True,
        )

    @check_all_players.error
    async def on_check_all_players_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.MissingAnyRole):
            await interaction.response.send_message(
                "Admin only.", ephemeral=True
            )
            return
        logger.warning("check_all_players error: %s", error, exc_info=True)
        if interaction.response.is_done():
            await interaction.followup.send("Command failed.", ephemeral=True)
        else:
            await interaction.response.send_message("Command failed.", ephemeral=True)
