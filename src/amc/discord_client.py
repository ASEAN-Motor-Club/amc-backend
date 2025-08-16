import discord
from discord.ext import commands
import aiohttp
from django.conf import settings
from amc.game_server import announce
from amc_cogs.moderation import ModerationCog
from amc_cogs.auth import AuthenticationCog
from amc_cogs.events import EventsCog
from amc_cogs.economy import EconomyCog

class AMCDiscordBot(commands.Bot):
  def __init__(self, *args, **kwargs):
    super().__init__(*args, command_prefix="/", **kwargs)

  async def setup_hook(self):
    self.http_client_game = aiohttp.ClientSession(base_url=settings.GAME_SERVER_API_URL)
    self.http_client_mod = aiohttp.ClientSession(base_url=settings.MOD_SERVER_API_URL)
    self.event_http_client_game = aiohttp.ClientSession(base_url=settings.EVENT_GAME_SERVER_API_URL)
    self.event_http_client_mod = aiohttp.ClientSession(base_url=settings.EVENT_MOD_SERVER_API_URL)
    guild = discord.Object(id=settings.DISCORD_GUILD_ID)
    await self.add_cog(ModerationCog(self), guild=guild)
    await self.add_cog(AuthenticationCog(self), guild=guild)
    await self.add_cog(EventsCog(self), guild=guild)
    await self.add_cog(EconomyCog(self), guild=guild)
    await self.tree.sync(guild=guild)

  async def on_message(self, message):
    if not message.author.bot and message.channel.id == settings.DISCORD_GAME_CHAT_CHANNEL_ID:
      await announce(
        f"{message.author.display_name}: {message.content}",
        self.http_client_game,
      )


intents = discord.Intents.default()
intents.messages = True
intents.members = True
intents.message_content = True
intents.guild_scheduled_events = True

bot = AMCDiscordBot(intents=intents)

