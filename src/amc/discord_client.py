import discord
from discord.ext import commands
import aiohttp
from django.conf import settings
from amc.game_server import announce

class AMCDiscordBot(commands.Bot):
  def __init__(self, *args, **kwargs):
    super().__init__(*args, command_prefix="/", **kwargs)

  async def setup_hook(self):
    self.http_client = aiohttp.ClientSession(base_url=settings.GAME_SERVER_API_URL)
    # Placeholder for regular announcements and other tasks

  async def on_message(self, message):
    if not message.author.bot and message.channel.id == settings.DISCORD_GAME_CHAT_CHANNEL_ID:
      await announce(
        f"{message.author.display_name}: {message.content}",
        self.http_client,
      )

intents = discord.Intents.default()
intents.messages = True
intents.members = True
intents.message_content = True

bot = AMCDiscordBot(intents=intents)
