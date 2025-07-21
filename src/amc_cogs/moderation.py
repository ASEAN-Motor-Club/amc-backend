from discord import app_commands
from discord.ext import commands
from amc.mod_server import show_popup

class ModerationCog(commands.Cog):
  def __init__(self, bot):
    self.bot = bot

  @app_commands.command(name='send_popup', description='Sends a popup message to an in-game player')
  @commands.has_permissions(administrator=True)
  async def send_popup(self, ctx, player_id: str, message: str):
    await show_popup(self.bot.http_client_mod, message, player_id=player_id)
    await ctx.response.send_message('Popup sent', ephemeral=True)

