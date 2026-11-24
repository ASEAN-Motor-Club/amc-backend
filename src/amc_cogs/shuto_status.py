"""Live-player board for the Shuto Motor Town instance.

Shares the prod ``#🟢-status`` channel with ``amc_cogs.status.StatusCog`` and
looks the same: ``Active Players`` title, Player Count + Players fields, blue
embed. Only the footer marks it as Shuto, which is also how the two cogs find
their own message after a restart instead of fighting over one message.
"""

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

if TYPE_CHECKING:
    from amc.discord_client import AMCDiscordBot

from django.conf import settings

from amc.game_server import game_api_request

logger = logging.getLogger(__name__)

# Same visible title/colour/layout as the prod board.
BOARD_TITLE = "Active Players"
BOARD_FOOTER = "Shuto — Updated every 30 seconds"


class ShutoStatusCog(commands.Cog):
    """Edit-in-place board showing who is online on the Shuto server."""

    def __init__(
        self,
        bot: "AMCDiscordBot",
        channel_id: int | None = None,
        api_password: str | None = None,
    ):
        self.bot = bot
        self.channel_id = (
            channel_id
            if channel_id is not None
            else settings.DISCORD_SHUTO_STATUS_CHANNEL_ID
        )
        self.api_password = (
            api_password
            if api_password is not None
            else settings.SHUTO_GAME_API_PASSWORD
        )
        self.last_embed_message = None

    async def cog_load(self):
        self.update_players.start()

    async def cog_unload(self):
        self.update_players.cancel()

    async def _fetch_players(self):
        """Return [(unique_id, player), ...] from the Shuto game web API.

        Call the API directly instead of ``get_players``: that helper caches
        under the global ``game_online_players_list`` Redis key, which the prod
        board also reads, so the two boards would show each other's players.
        """
        data = await game_api_request(
            self.bot.shuto_http_client_game,
            "/player/list",
            password=self.api_password,
        )
        if not data or data.get("succeeded") is False:
            raise RuntimeError(f"Shuto game API error: {data!r}")
        players = data.get("data") or {}
        return [
            (player["unique_id"], player)
            for player in players.values()
            if player is not None
        ]

    def _build_embed(self, players, offline: bool = False):
        embed = discord.Embed(
            title=BOARD_TITLE,
            color=discord.Color.red() if offline else discord.Color.blue(),
            timestamp=discord.utils.utcnow(),
        )
        if offline:
            embed.add_field(
                name="Status", value="Shuto server unreachable", inline=False
            )
        else:
            embed.add_field(
                name="Player Count", value=str(len(players)), inline=False
            )
            if players:
                players_str = "\n".join(
                    discord.utils.escape_markdown(p.get("name", "?"))
                    for _, p in players
                )
                if len(players_str) > 1000:
                    players_str = players_str[:1000] + "... (truncated)"
            else:
                players_str = "No players online"
            embed.add_field(name="Players", value=players_str, inline=False)
        embed.set_footer(text=BOARD_FOOTER)
        return embed

    async def _upsert_message(self, channel, embed):
        if self.last_embed_message is None:
            async for message in channel.history(limit=10):
                if message.author == self.bot.user and any(
                    e.title == BOARD_TITLE and (e.footer.text or "") == BOARD_FOOTER
                    for e in message.embeds
                ):
                    self.last_embed_message = message
                    break

        if self.last_embed_message is not None:
            try:
                await self.last_embed_message.edit(embed=embed)
                return
            except discord.NotFound:
                self.last_embed_message = None
            except discord.HTTPException:
                logger.exception("Failed to edit Shuto board message")
                return

        self.last_embed_message = await channel.send(embed=embed)

    @tasks.loop(seconds=30)
    async def update_players(self):
        channel = self.bot.get_channel(self.channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return

        try:
            players = await self._fetch_players()
        except Exception:
            logger.exception("Shuto player fetch failed")
            embed = self._build_embed([], offline=True)
        else:
            embed = self._build_embed(players)

        await self._upsert_message(channel, embed)

    @update_players.before_loop
    async def before_update_players(self):
        await self.bot.wait_until_ready()

    @update_players.error
    async def update_players_error(self, error):
        logger.exception("Shuto board loop error, restarting in 30s", exc_info=error)
        self.update_players.restart()
