import discord
from discord.ext import tasks, commands
from django.conf import settings
from amc.game_server import get_players


class StatusCog(commands.Cog):
  def __init__(self, bot, status_channel_id=settings.DISCORD_STATUS_CHANNEL_ID):
    self.bot = bot
    self.status_channel_id = status_channel_id
    self.last_embed_message = None

  async def cog_load(self):
    self.update_status_embed.start()

  async def cog_unload(self):
    self.update_status_embed.cancel()

  @tasks.loop(seconds=60)
  async def update_status_embed(self):
    """
    Fetch the latest active players and update the embed message in the channel.
    """
    client = self.bot

    channel = client.get_channel(self.status_channel_id)
    if channel is None:
      print("Channel not found.")
      return

    # Retrieve active players from the API
    active_players = await get_players(self.bot.http_client_game)
    count = len(active_players)

    embed = discord.Embed(title="Active Players", color=discord.Color.blue())
    embed.add_field(name="Live map", value="[Open on the website](https://www.aseanmotorclub.com/map)", inline=False)
    embed.add_field(name="Player Count", value=str(count), inline=False)
    if active_players:
      embed.add_field(name="Players", value="\n".join([player_name for player_id, player_name in active_players]), inline=False)
    else:
      embed.add_field(name="Players", value="No active players", inline=False)

    if self.last_embed_message is None:
      async for message in channel.history(limit=1):
        self.last_embed_message = message
      if self.last_embed_message:
        await self.last_embed_message.edit(embed=embed)
      else:
        self.last_embed_message = await channel.send(embed=embed)
    else:
      try:
        await self.last_embed_message.edit(embed=embed)
      except discord.NotFound:
        self.last_embed_message = await channel.send(embed=embed)

  @update_status_embed.before_loop
  async def before_update_status_embed(self):
    await self.bot.wait_until_ready()

