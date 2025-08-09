from discord import app_commands
from discord.ext import commands
from django.db.models import Q
from amc.models import Player
from amc.mod_server import show_popup

class ModerationCog(commands.Cog):
  def __init__(self, bot):
    self.bot = bot

  @app_commands.command(name='send_popup', description='Sends a popup message to an in-game player')
  @app_commands.checks.has_any_role(1395460420189421713)
  async def send_popup(self, ctx, player_id: str, message: str):
    player = await Player.objects.aget(
      Q(unique_id=player_id) | Q(discord_user_id=player_id)
    )
    await show_popup(self.bot.http_client_mod, message, player_id=player.unique_id)
    await ctx.response.send_message(f'Popup sent to {player.unique_id}: {message}')

