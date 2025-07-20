import discord
import aiohttp
from django.conf import settings
from amc.game_server import announce

class AMCDiscordClient(discord.Client):
  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)

  async def setup_hook(self):
    self.http_client = aiohttp.ClientSession(base_url=settings.GAME_SERVER_API_URL)
    # Placeholder for regular announcements and other tasks

  async def on_message(self, message):
    if not message.author.bot and message.channel.id == settings.DISCORD_GAME_CHAT_CHANNEL_ID:
      await announce(
        f"{message.author.display_name}: {message.content}",
        self.http_client,
      )

