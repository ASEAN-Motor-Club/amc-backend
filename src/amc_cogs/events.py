import re
from django.db.models import StdDev
from discord import app_commands
import discord
import hashlib
import hmac
from random import Random
from discord.ext import commands
from django.conf import settings
from amc.models import GameEventCharacter
from .utils import create_player_autocomplete
from amc.mod_server import join_player_to_event, kick_player_from_event, get_events

from amc.models import (
  ScheduledEvent,
  Team,
  Player,
  Championship,
  ChampionshipPoint,
)

def format_time(total_seconds: float) -> str:
  if total_seconds is None or total_seconds < 0:
    return "-"
  """Converts seconds (float) into MM:SS.sss format.

  Args:
    total_seconds: The total number of seconds as a float.

  Returns:
    A string representing the time in MM:SS.sss format.
  """
  if not isinstance(total_seconds, (int, float)):
    raise TypeError("Input must be a number (int or float).")
  if total_seconds < 0:
    raise ValueError("Input seconds cannot be negative.")

  minutes = int(total_seconds // 60)
  seconds = total_seconds % 60

  # Format minutes to always have two digits
  formatted_minutes = f"{minutes:02d}"

  # Format seconds to have two digits for the integer part
  # and three digits for the fractional part
  formatted_seconds = f"{seconds:06.3f}" # 06.3f ensures XX.YYY format

  return f"{formatted_minutes}:{formatted_seconds}"

def generate_deterministic_penalty(
  seed_string: str, 
  min_penalty: float, 
  max_penalty: float
) -> float:
  """
  Generates a deterministic, pseudo-random float penalty based on an input string.
  """

  if not settings.SECRET_KEY:
      raise ValueError("Django SECRET_KEY is not configured.")
      
  if min_penalty > max_penalty:
      raise ValueError("min_penalty cannot be greater than max_penalty.")

  # 1. Get the secret key and the input string as bytes.
  # HMAC works with bytes, so we encode them.
  key = bytes(settings.SECRET_KEY, 'utf-8')
  msg = bytes(seed_string, 'utf-8')

  # 2. Create a keyed hash (HMAC) using the secret key.
  # This is more secure than a simple hash as it involves the secret key.
  # The result is a unique and unpredictable (without the key) byte string.
  hmac_digest = hmac.new(key, msg, hashlib.sha256).digest()

  # 3. Convert the resulting hash bytes to an integer.
  # This integer will be the seed for our random number generator.
  # 'big' means the most significant byte is at the beginning of the byte array.
  seed_integer = int.from_bytes(hmac_digest, 'big')

  # 4. Create a local Random instance seeded with our integer.
  # Using a local instance prevents this function from interfering with
  # other parts of your Django application that might rely on the global
  # random state (e.g., for generating CSRF tokens).
  random_instance = Random(seed_integer)

  # 5. Generate and return a uniform float value in the desired range.
  penalty = random_instance.uniform(min_penalty, max_penalty)
  
  return penalty

class EventsCog(commands.Cog):
  def __init__(self, bot, teams_channel_id=settings.DISCORD_TEAMS_CHANNEL_ID):
    self.bot = bot
    self.teams_channel_id = teams_channel_id
    self.last_embed_message = None
    self.player_autocomplete = create_player_autocomplete(self.bot.event_http_client_game)

  @commands.Cog.listener()
  async def on_ready(self):
    await self.sync_teams()
    await self.update_championship_standings()

  @commands.Cog.listener()
  async def on_thread_create(self, thread):
    await self.thread_to_team(thread)

  @commands.Cog.listener()
  async def on_reaction_add(self, reaction, user):
    thread = reaction.message.thread
    if thread and \
      thread.parent and \
      thread.parent.id == self.teams_channel_id and \
      reaction.emoji == "🏎️":
      try:
        team = await Team.objects.aget(
          discord_thread_id=reaction.message.thread
        )
        player = await Player.objects.aget(
          discord_user_id=user.id
        )
        await team.players.aadd(player)
      except Team.DoesNotExist:
        pass
      except Player.DoesNotExist:
        pass

  @commands.Cog.listener()
  async def on_reaction_remove(self, reaction, user):
    thread = reaction.message.thread
    if thread and \
      thread.parent and \
      thread.parent.id == self.teams_channel_id and \
      reaction.emoji == "🏎️":
      try:
        team = await Team.objects.aget(
          discord_thread_id=reaction.message.thread
        )
        player = await Player.objects.aget(
          discord_user_id=user.id
        )
        await team.players.aremove(player)
      except Team.DoesNotExist:
        pass
      except Player.DoesNotExist:
        pass

  @commands.Cog.listener()
  async def on_scheduled_event_update(self, before, after):
    await ScheduledEvent.objects.aupdate_or_create(
      discord_event_id=after.id,
      defaults={
        'name': after.name,
        'start_time': after.start_time,
        'end_time': after.end_time,
        'description': after.description,
      }
    )

  @commands.Cog.listener()
  async def on_scheduled_event_create(self, event):
    await ScheduledEvent.objects.acreate(
      discord_event_id=event.id,
      name=event.name,
      start_time=event.start_time,
      end_time=event.end_time,
      description=event.description,
    )

  async def thread_to_team(self, thread):
    name_match = re.match(r'\[(?P<tag>\w+)\](?P<name>.+)', thread.name)
    if not name_match:
      return

    team, _ = await Team.objects.aupdate_or_create(
      discord_thread_id=thread.id,
      defaults={
        'name': name_match.group('name').strip(),
        'description': thread.starter_message.content if thread.starter_message else '',
        'tag': name_match.group('tag'),
      }
    )
    try:
      owner_player = await Player.objects.aget(discord_user_id=thread.owner_id)
      await team.owners.aadd(owner_player)
    except Player.DoesNotExist:
      pass

    starter_message = await thread.fetch_message(thread.id)
    for reaction in starter_message.reactions:
      if reaction.emoji == "🏎️":
        players = []
        async for user in reaction.users():
          try:
            player = await Player.objects.aget(discord_user_id=user.id)
            players.append(player)
          except Player.DoesNotExist:
            pass
        await team.players.aadd(*players)

  async def sync_teams(self):
    client = self.bot
    forum_channel = client.get_channel(self.teams_channel_id)
    threads = forum_channel.threads

    for thread in threads:
      await self.thread_to_team(thread)

  async def update_scheduled_event_embed(self, scheduled_event_id):
    scheduled_event = await ScheduledEvent.objects.select_related('race_setup').aget(pk=scheduled_event_id)
    race_setup = scheduled_event.race_setup

    embed = discord.Embed(
      title=f"{scheduled_event.name} - Results",
      color=discord.Color.yellow(),  # You can choose any color
    )
    participant_list_str = ""
    participants = [p async for p in GameEventCharacter.objects.results_for_scheduled_event(scheduled_event)]
    for rank, participant in enumerate(participants, start=1):
      if participant.finished:
        progress_str = format_time(participant.net_time)
      else:
        total_laps = max(race_setup.num_laps, 1)
        total_waypoints = race_setup.num_sections

        if race_setup.num_laps == 0:
          total_waypoints = total_waypoints - 1

        progress_percentage = 0.0
        if total_waypoints > 0:
          progress_percentage = 100.0 * max(participant.laps - 1, 0) / total_laps
          progress_percentage += 100.0 * max(participant.section_index, 0) / float(total_waypoints) / total_laps
        if race_setup.num_laps > 0:
          progress_str = f"{participant.laps}/{race_setup.num_laps} Laps - {progress_percentage:.1f}%"
        else:
          progress_str = f"{progress_percentage:.1f}%"

      participant_line = f"{rank}. {participant.character.name} ({progress_str})"

      if participant.wrong_vehicle:
        participant_line += " [Wrong Vehicle]"
      if participant.wrong_engine:
        participant_line += " [Wrong Engine]"

      participant_list_str += f"{participant_line}\n"

    embed.add_field(name="👥 Latest Results", value=participant_list_str.strip(), inline=False)

    channel = self.bot.get_channel(settings.DISCORD_CHAMPIONSHIP_CHANNEL_ID)
    if scheduled_event.discord_message_id:
      try:
        message = await channel.fetch_message(scheduled_event.discord_message_id)
        await message.edit(embed=embed)
      except discord.NotFound:
        # Message was deleted in Discord. Clear the invalid ID.
        # It will be recreated in the CREATE path below.
        scheduled_event.discord_message_id = None
      except Exception as e:
        print(f"Error updating message for scheduled_event {scheduled_event.id}: {e}")

    # CREATE path
    if not scheduled_event.discord_message_id:
      new_message = await channel.send(embed=embed)
      scheduled_event.discord_message_id = new_message.id
      await scheduled_event.asave(update_fields=['discord_message_id'])

  async def update_championship_standings(self):
    championship = await Championship.objects.alast()
    if not championship:
      return
    personal_standings = [s async for s in ChampionshipPoint.objects.personal_standings(championship)]
    team_standings = [s async for s in ChampionshipPoint.objects.team_standings(championship)]
    
    embed = discord.Embed(
      title=f"{championship.name} Standings",
      description="[See more details on the website](https://www.aseanmotorclub.com/championship/details)",
      color=discord.Color.yellow(),  # You can choose any color
    )
    team_standings_str = '\n'.join([
      f"{str(rank).rjust(2)}. {s['team__tag'].ljust(6)} {s['team__name'].ljust(30)} {str(s['total_points']).rjust(3)}"
      for rank, s in enumerate(team_standings, start=1)
      if s['total_points'] > 0
    ])
    embed.add_field(
      name="Team Standings",
      value=f"```\n{team_standings_str}\n```",
      inline=False
    )
    personal_standings_str = '\n'.join([
      f"{str(rank).rjust(2)}. {s['character_name'].ljust(16)} {str(s['total_points']).rjust(3)}"
      for rank, s in enumerate(personal_standings, start=1)
      if s['total_points'] > 0
    ])
    embed.add_field(
      name="Personal Standings",
      value=f"```\n{personal_standings_str}\n```",
      inline=False
    )

    last_embed_message = self.last_embed_message
    channel = self.bot.get_channel(settings.DISCORD_CHAMPIONSHIP_CHANNEL_ID)
    if last_embed_message is None:
        async for message in channel.history(limit=1, oldest_first=True):
          last_embed_message = message
        if last_embed_message:
          await last_embed_message.edit(embed=embed)
        else:
          last_embed_message = await channel.send(embed=embed)
    else:
        try:
            await last_embed_message.edit(embed=embed)
        except discord.NotFound:
            # In case the message was deleted, send a new one
            last_embed_message = await channel.send(embed=embed)

  @app_commands.command(name='calculate_stddev', description='Get the standard deviation of race results')
  async def calculate_stddev(self, interaction, scheduled_event_id: int):
    aggregates = await (GameEventCharacter.objects
      .filter(game_event__scheduled_event=scheduled_event_id, finished=True)
      .aaggregate(stddev=StdDev('net_time'))
    )
    stddev = aggregates['stddev']
    await interaction.response.send_message(f"Standard Deviation: {stddev} seconds")

  @app_commands.command(name='calculate_event_penalty', description='Deterministically calculate a penalty based on a range')
  async def calculate_event_penalty(self, interaction, scheduled_event_id: int, seed: str):
    aggregates = await (GameEventCharacter.objects
      .filter(game_event__scheduled_event=scheduled_event_id, finished=True)
      .aaggregate(stddev=StdDev('net_time'))
    )
    stddev = aggregates['stddev']
    penalty = generate_deterministic_penalty(
      f"{seed}:{interaction.user.id}:{scheduled_event_id}",
      stddev*0.5,
      stddev*1.5
    )
    await interaction.response.send_message(f"Penalty: {penalty}")

  async def player_autocomplete(self, interaction, current):
    return await self.player_autocomplete(interaction, current)

  @app_commands.command(name='join_player_to_event', description='Joins a player into an event (Event Server)')
  @app_commands.checks.has_any_role(1395460420189421713)
  @app_commands.autocomplete(player_id=player_autocomplete)
  async def join_player_to_event(self, ctx, player_id: str):
    events = await get_events(self.bot.event_http_client_mod)
    if not events:
      await ctx.response.send_message('No active events')
      return

    event = events[0]
    await join_player_to_event(self.bot.event_http_client_mod, event['EventGuid'], player_id)
    await ctx.response.send_message(f'Player {player_id} joined')

  @app_commands.command(name='kick_player_from_event', description='Kicks a player from an event (Event Server)')
  @app_commands.checks.has_any_role(1395460420189421713)
  @app_commands.autocomplete(player_id=player_autocomplete)
  async def kick_player_from_event(self, ctx, player_id: str):
    events = await get_events(self.bot.event_http_client_mod)
    if not events:
      await ctx.response.send_message('No active events')
      return

    event = events[0]
    await kick_player_from_event(self.bot.event_http_client_mod, event['EventGuid'], player_id)
    await ctx.response.send_message(f'Player {player_id} kicked')

  @app_commands.command(name='post_scheduled_event_embed', description='Creates a scheduled event embed')
  @app_commands.checks.has_permissions(administrator=True)
  async def post_scheduled_event_embed(self, ctx, scheduled_event_id: str):
    await self.update_scheduled_event_embed(int(scheduled_event_id))

