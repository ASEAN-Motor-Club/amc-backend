import os
from discord.ext import commands
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from amc.discord_client import AMCDiscordBot
from django.conf import settings
from amc.models import Player
from amc.mod_server import send_message_as_player
from amc.game_server import announce, get_players

FIFO_PATH = os.environ.get('NECESSE_FIFO_PATH')

class ChatCog(commands.Cog):
  def __init__(self, bot: "AMCDiscordBot", game_chat_channel_id=settings.DISCORD_GAME_CHAT_CHANNEL_ID):
    self.bot = bot
    self.game_chat_channel_id = game_chat_channel_id

  @commands.Cog.listener()
  async def on_message(self, message):
    if not message.author.bot and message.channel.id == self.game_chat_channel_id:
      try:
        player = await Player.objects.aget(discord_user_id=message.author.id)
        online_players = await get_players(self.bot.http_client_game)
        online_players_by_id = {str(uid): data['name'] for uid, data in online_players}
        if str(player.unique_id) in online_players_by_id:
          await send_message_as_player(self.bot.http_client_mod, message.content, str(player.unique_id))
          return
      except Player.DoesNotExist:
        pass

      await announce(
        f"{message.author.display_name}: {message.content}",
        self.bot.http_client_game,
        color="FFFFFF",
      )

    if not message.author.bot and message.channel.id == settings.DISCORD_NECESSE_GAME_CHAT_CHANNEL_ID:
      if not FIFO_PATH:
        return
      try:
        with open(FIFO_PATH, 'w') as f:
          f.write(f"/print {message.author.display_name}: {message.content}\n")
          f.flush()
      except PermissionError:
        print(f"Error: Permission denied accessing {FIFO_PATH}.")
        print("Ensure your user is in the 'modders' group or listed in 'commandUsers'.")
        await message.channel.send(f"Error: Permission denied accessing {FIFO_PATH}.")
      except OSError as e:
        print(f"OS Error: {e}")
        await message.channel.send(f"OS Error: {e}")
      except Exception as e:
        await message.channel.send(f"Exception: {e}")

