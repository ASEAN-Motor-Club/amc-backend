import logging

import discord
from discord.ext import tasks, commands
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from amc.discord_client import AMCDiscordBot
from django.conf import settings
from amc.beammp import query_beammp

logger = logging.getLogger(__name__)


class BeamMPStatusCog(commands.Cog):
    def __init__(
        self,
        bot: "AMCDiscordBot",
        beammp_channel_id=settings.DISCORD_BEAMMP_STATUS_CHANNEL_ID,
    ):
        self.bot = bot
        self.beammp_channel_id = beammp_channel_id
        self.last_embed_message = None

    async def cog_load(self):
        self.update_status.start()

    async def cog_unload(self):
        self.update_status.cancel()

    @tasks.loop(seconds=30)
    async def update_status(self):
        channel = self.bot.get_channel(self.beammp_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return

        info = await query_beammp(
            settings.BEAMMP_SERVER_HOST,
            settings.BEAMMP_SERVER_PORT,
        )
        if info is None:
            return

        embed = discord.Embed(
            title=info["name"],
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Player Count",
            value=f"{info['players']}/{info['max_players']}",
            inline=True,
        )

        if info["player_names"]:
            players_str = "\n".join(
                discord.utils.escape_markdown(n) for n in info["player_names"]
            )
            if len(players_str) > 1000:
                players_str = players_str[:1000] + "... (truncated)"
        else:
            players_str = "No players online"
        embed.add_field(name="Players", value=players_str, inline=False)

        if info["map"]:
            embed.add_field(name="Map", value=info["map"], inline=True)
        if info["version"]:
            embed.add_field(name="Version", value=info["version"], inline=True)
        if info["mods_total"]:
            embed.add_field(
                name="Mods", value=str(info["mods_total"]), inline=True
            )

        embed.set_footer(text="Updated every 30 seconds")

        if self.last_embed_message is None:
            async for message in channel.history(limit=5):
                if message.author == self.bot.user:
                    self.last_embed_message = message
                    break

        if self.last_embed_message:
            try:
                await self.last_embed_message.edit(embed=embed)
            except (discord.NotFound, discord.HTTPException):
                self.last_embed_message = await channel.send(embed=embed)
            except discord.Forbidden:
                logger.exception("Forbidden to edit BeamMP message")
                self.last_embed_message = await channel.send(embed=embed)
        else:
            self.last_embed_message = await channel.send(embed=embed)

    @update_status.before_loop
    async def before_update_status(self):
        await self.bot.wait_until_ready()

    @update_status.error
    async def update_status_error(self, error):
        logger.exception("BeamMP status loop error, restarting in 30s")
        self.update_status.restart()
