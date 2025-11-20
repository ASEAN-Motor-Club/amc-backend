import asyncio
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
    self.fps_data = []
    self.memory_data = []

  async def cog_load(self):
    self.update_status_embed.start()
    self.daily_top_restockers_task.start()

  async def cog_unload(self):
    self.update_status_embed.cancel()

  def generate_graph_image(self, fps_data: list, memory_data: list) -> io.BytesIO:
    """Generates the dual-axis line graph image using Matplotlib."""
    plt.style.use('dark_background')
    fig, ax1 = plt.subplots()

    # Plot FPS data on the primary y-axis (left)
    color_fps = 'cyan'
    ax1.set_xlabel("Time (Updates)", color='white')
    ax1.set_ylabel("FPS", color=color_fps)
    ax1.plot(fps_data, color=color_fps, marker='o', label='FPS')
    ax1.tick_params(axis='y', labelcolor=color_fps)
    ax1.set_ylim(0, 120)
    ax1.grid(True, linestyle='--', alpha=0.6)

    # Create a second y-axis for memory data that shares the x-axis
    ax2 = ax1.twinx()
    color_mem = 'lime'
    ax2.set_ylabel("Used Memory (GB)", color=color_mem)
    ax2.plot(memory_data, color=color_mem, marker='x', label='Memory (GB)')
    ax2.tick_params(axis='y', labelcolor=color_mem)
    ax2.set_ylim(0, 32)
    # You might want to set a ylim for memory as well, e.g., ax2.set_ylim(0, 8192)

    # Add a title and adjust layout
    fig.suptitle("Live Server Status", color='white')
    fig.tight_layout(rect=[0, 0.03, 1, 0.95]) # Adjust rect to make space for title

    # Create a combined legend for both lines
    lines, labels = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines + lines2, labels + labels2, loc='upper left')

    # Save the plot to a BytesIO buffer
    buffer = io.BytesIO()
    plt.savefig(buffer, format='png', transparent=True)
    buffer.seek(0)
    plt.close(fig) # Close the figure to free up memory

    return buffer

  @tasks.loop(seconds=30)
  async def update_status_embed(self):
    """
    Fetches server data, generates the graph non-blockingly, and updates the embed.
    """
    # 1. Basic Safety Checks
    channel = self.bot.get_channel(self.status_channel_id)
    if not channel:
      print("Status channel not found.")
      return

    try:
      # 2. Robust Data Fetching (Wrapped in try/except for stability)
      try:
        active_players = await get_players(self.bot.http_client_game)
      except Exception as e:
        print(f"Failed to fetch players: {e}")
        active_players = []

      count = len(active_players)

      # DB Fetching
      try:
        # Eagerly evaluate the async generator and reverse it
        statuses = ServerStatus.objects.all().order_by('-timestamp')[:60]
        fetched_statuses = [status async for status in statuses][::-1]
        
        self.fps_data = [status.fps for status in fetched_statuses]
        self.memory_data = [status.used_memory / 1073741824 for status in fetched_statuses]
      except Exception as e:
        print(f"Failed to fetch DB status: {e}")
        # Keep old data if DB fails
        if not hasattr(self, 'fps_data'):
          self.fps_data = []
        if not hasattr(self, 'memory_data'):
          self.memory_data = []

      # 3. Non-Blocking Graph Generation (CRITICAL FIX: Offload sync code)
      # Uses the default executor (thread pool) to prevent blocking the event loop.
      graph_buffer = await asyncio.to_thread(
        self.generate_graph_image, 
        self.fps_data, 
        self.memory_data
      )
      
      graph_buffer.seek(0) # Reset pointer to start of buffer
      graph_file = discord.File(graph_buffer, filename="status_graph.png")

      # 4. Build Embed
      embed = discord.Embed(
        title="Active Players",
        color=discord.Color.blue(),
        timestamp=discord.utils.utcnow(),
      )
      # Link the attachment inside the embed
      embed.set_image(url="attachment://status_graph.png") 
      embed.add_field(name="Live map", value="[Open on the website](https://www.aseanmotorclub.com/map)", inline=False)
      embed.add_field(name="Player Count", value=str(count), inline=False)
      embed.set_footer(text="Updated every 30 seconds")
      
      # Player List
      if active_players:
        players_str = "\n".join([
          discord.utils.escape_markdown(p[1]) for p in active_players
        ])
        if len(players_str) > 1000:
            players_str = players_str[:1000] + "... (truncated)"
        
        embed.add_field(name="Players", value=players_str, inline=False)
      else:
        embed.add_field(name="Players", value="No active players", inline=False)

      # 5. Message Handling Logic (CRASH FIX: Check author)
      
      # If we don't have a tracked message, look for the bot's last message
      if self.last_embed_message is None:
        async for message in channel.history(limit=5):
          if message.author == self.bot.user: # Only grab messages the bot owns
            self.last_embed_message = message
            break
      
      # Action: Edit or Send
      if self.last_embed_message:
        try:
          await self.last_embed_message.edit(embed=embed, attachments=[graph_file])
        except discord.NotFound:
          # Message was deleted: send a new one
          self.last_embed_message = await channel.send(embed=embed, file=graph_file)
        except discord.Forbidden:
          # Message found, but we can't edit it (e.g., permissions changed or logic error)
          print("Forbidden to edit message. Sending new one.")
          self.last_embed_message = await channel.send(embed=embed, file=graph_file)
      else:
        self.last_embed_message = await channel.send(embed=embed, file=graph_file)

    except Exception as e:
      # Final catch-all to log any unhandled error and prevent silent death
      print(f"🔥 Status Loop Iteration Failed Unrecoverably: {e}")

  @update_status_embed.before_loop
  async def before_update_status_embed(self):
    await self.bot.wait_until_ready()

  @update_status_embed.error
  async def update_status_embed_error(self, error):
    print(f"Status Loop Error: {error}")

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
