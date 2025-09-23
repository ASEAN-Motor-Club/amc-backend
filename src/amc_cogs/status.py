import discord
import io
import matplotlib.pyplot as plt
from datetime import time as dt_time, timedelta, timezone as dt_timezone
from django.utils import timezone
from django.db.models import Count, Q
from discord import app_commands
from discord.ext import tasks, commands
from django.conf import settings
from amc.game_server import get_players
from amc.models import Character, ServerStatus


class StatusCog(commands.Cog):
  def __init__(
    self,
    bot,
    status_channel_id=settings.DISCORD_STATUS_CHANNEL_ID,
    general_channel_id=settings.DISCORD_GENERAL_CHANNEL_ID,
  ):
    self.bot = bot
    self.status_channel_id = status_channel_id
    self.general_channel_id = general_channel_id
    self.last_embed_message = None
    self.data_points = []

  async def cog_load(self):
    self.update_status_embed.start()
    self.daily_top_restockers_task.start()

  async def cog_unload(self):
    self.update_status_embed.cancel()

  def generate_graph_image(self) -> io.BytesIO:
    """Generates the line graph image using Matplotlib."""
    plt.style.use('dark_background') # Use a discord-friendly style
    fig, ax = plt.subplots()

    ax.plot(self.data_points, color='cyan', marker='o')
    ax.set_title("Live Server FPS", color='white')
    ax.set_ylabel("fps", color='white')
    ax.set_xlabel("Time (Updates)", color='white')
    ax.grid(True, linestyle='--', alpha=0.6)
    
    # Save the plot to a BytesIO buffer
    buffer = io.BytesIO()
    plt.savefig(buffer, format='png', transparent=True)
    buffer.seek(0)
    plt.close(fig) # Close the figure to free up memory

    return buffer

  @tasks.loop(seconds=2)
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

    statuses = ServerStatus.objects.all().order_by('-timestamp')[:60]
    self.data_points = [
      status.fps
      async for status in statuses
    ][::-1]

    graph_buffer = self.generate_graph_image()
    graph_file = discord.File(graph_buffer, filename="fps.png")

    embed = discord.Embed(
      title="Active Players",
      color=discord.Color.blue(),
      timestamp=timezone.now(),
    )
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
        await self.last_embed_message.edit(embed=embed, attachments=[graph_file])
      else:
        self.last_embed_message = await channel.send(embed=embed, file=graph_file)
    else:
      try:
        await self.last_embed_message.edit(embed=embed, attachments=[graph_file])
      except discord.NotFound:
        self.last_embed_message = await channel.send(embed=embed, file=graph_file)

  @update_status_embed.before_loop
  async def before_update_status_embed(self):
    await self.bot.wait_until_ready()

  @tasks.loop(time=dt_time(hour=2, minute=0, tzinfo=dt_timezone.utc))
  async def daily_top_restockers_task(self):
    top_restockers_str = await self.daily_top_restockers()
    client = self.bot
    general_channel = client.get_channel(self.general_channel_id)
    await general_channel.send(f"""\
## Top 3 Depot Restockers
Last 24 hours

{top_restockers_str}

Thank you for your service!""")


  @daily_top_restockers_task.before_loop
  async def before_daily_top_restockers(self):
    await self.bot.wait_until_ready()

  @app_commands.command(name='list_top_depot_restockers', description='Get the list of top depot restockers')
  async def daily_top_restockers_cmd(self, interaction, days: int=1, top_n: int=3):
    top_restockers_str = await self.daily_top_restockers(days=days, top_n=top_n)
    await interaction.response.send_message(f"""\
## Top 3 Depot Restockers
Last 24 hours

{top_restockers_str}

Thank you for your service!""")

  async def daily_top_restockers(self, days=1, top_n=3):
    now = timezone.now()

    qs = Character.objects.annotate(
      depots_restocked=Count(
        'restock_depot_logs',
        distinct=True,
        filter=Q(restock_depot_logs__timestamp__gte=now - timedelta(days=days))
      ),
    ).filter(depots_restocked__gt=0).order_by('-depots_restocked')[:top_n]
    top_restockers_str = '\n'.join([
      f"@{character.name} - {character.depots_restocked}"
      async for character in qs
    ])
    return top_restockers_str

