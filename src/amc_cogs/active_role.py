"""Admin utilities for the daily Active role sync (see amc.active_role)."""

import logging

import discord
from discord import app_commands
from discord.ext import commands
from django.conf import settings

from amc.active_role import sync_active_role

logger = logging.getLogger(__name__)


class ActiveRoleCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        admin_role_id = int(settings.DISCORD_ADMIN_ROLE_ID or 0)
        if not admin_role_id:
            return False
        if not isinstance(interaction.user, discord.Member):
            return False
        role = interaction.guild.get_role(admin_role_id) if interaction.guild else None
        return bool(role and role in interaction.user.roles)

    @app_commands.command(
        name="active_sync",
        description="Admin: run the Active role sync right now",
    )
    async def active_sync(self, interaction: discord.Interaction):
        if not self._is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        # The DB query + role churn can exceed 2s — defer, then follow up.
        await interaction.response.defer(ephemeral=True)
        summary = await sync_active_role(self.bot)
        if summary.get("skipped"):
            await interaction.followup.send(
                "Active role sync skipped — role/guild not configured."
            )
            return
        await interaction.followup.send(
            f"✅ Active sync done — added **{summary['added']}**, "
            f"removed **{summary['removed']}**, "
            f"missing from guild: {summary['missing']}."
        )
