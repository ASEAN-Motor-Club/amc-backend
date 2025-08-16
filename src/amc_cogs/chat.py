from discord.ext import commands
from django.conf import settings
from amc.models import Player
from amc.mod_server import send_message_as_player
from amc.game_server import announce, get_players

class ChatCog(commands.Cog):
  def __init__(self, bot, game_chat_channel_id=settings.DISCORD_GAME_CHAT_CHANNEL_ID):
    self.bot = bot
    self.game_chat_channel_id = game_chat_channel_id

  @commands.Cog.listener()
  async def on_message(self, message):
    if not message.author.bot and message.channel.id == self.game_chat_channel_id:
      try:
        player = await Player.objects.aget(discord_user_id=message.author.id)
        online_players = await get_players(self.bot.http_client_game)
        online_players_by_id = dict(online_players)
        if str(player.unique_id) in online_players_by_id:
          await send_message_as_player(self.bot.http_client_mod, message.content, str(player.unique_id))
          return
      except Player.DoesNotExist:
        pass

      await announce(f"{message.author.display_name}: {message.content}", self.bot.http_client_game)

