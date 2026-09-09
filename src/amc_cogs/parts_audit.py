"""/check_all_players — silent parts audit over an event's participants.

Resolves one Ready/Racing event (explicit name/GUID-prefix argument, else
the most recent one) and posts one embed per participant (power / engine /
intake / turbo / tires summary, same lines as the event-join audit) to
``DISCORD_PARTS_LOG_CHANNEL_ID``.  Admin-gated
(``DISCORD_ADMIN_ROLE_ID``) — the ephemeral confirmation reports exactly
how many reports were actually delivered, so a permissions problem on the
(private) log channel is visible instead of silent.
"""

import logging

import discord
from django.conf import settings
from discord import app_commands
from discord.ext import commands
from django.db.models import Q

from amc.models import GameEvent, GameEventCharacter
from amc.parts_audit import audit_character

logger = logging.getLogger(__name__)


async def _resolve_event(event_query: str | None):
    """Find the target Ready/Racing event.

    *event_query* matches GameEvent GUID prefix or name substring; without
    it the most recent Ready/Racing event wins.  Returns None when nothing
    matches.
    """
    qs = GameEvent.objects.filter(state__in=[1, 2])
    if event_query:
        qs = qs.filter(Q(guid__istartswith=event_query) | Q(name__icontains=event_query))
    return await qs.order_by("-start_time").afirst()


class PartsAuditCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="check_all_players",
        description="Silent parts check on every player in an event (posts to the parts-log channel)",
    )
    @app_commands.describe(
        event="Event name or GUID prefix (defaults to the most recent Ready/Racing event)",
    )
    @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
    async def check_all_players(
        self, interaction: discord.Interaction, event: str | None = None
    ):
        await interaction.response.defer(ephemeral=True)

        game_event = await _resolve_event(event)
        if game_event is None:
            await interaction.followup.send(
                "No Ready/Racing event found"
                + (f" matching `{event}`." if event else "."),
                ephemeral=True,
            )
            return

        participants = [
            (gec.character.guid, gec.character.name)
            async for gec in GameEventCharacter.objects.filter(
                game_event=game_event
            ).select_related("character")
            if gec.character.guid
        ]
        if not participants:
            await interaction.followup.send(
                f"Event `{game_event.name}` has no recorded participants.",
                ephemeral=True,
            )
            return

        source = f"event check: {game_event.name}"
        posted = 0
        for guid, player_name in participants:
            result = await audit_character(
                self.bot.http_client_mod,
                guid,
                player_name,
                discord_client=self.bot,
                source=source,
            )
            if result is not None:
                posted += 1

        note = ""
        if posted == 0 and settings.DISCORD_PARTS_LOG_CHANNEL_ID:
            note = (
                " — could not deliver to <#%s>: the bot may lack View Channel/"
                "Send Messages/Embed Links access there."
                % settings.DISCORD_PARTS_LOG_CHANNEL_ID
            )
        elif not settings.DISCORD_PARTS_LOG_CHANNEL_ID:
            note = " (log channel not configured — summaries in worker journal only)"

        await interaction.followup.send(
            f"Event `{game_event.name}`: checked {len(participants)} participant(s), "
            f"{posted} report(s) delivered{note}.",
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
