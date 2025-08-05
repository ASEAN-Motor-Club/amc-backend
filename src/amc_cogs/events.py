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

from amc.models import (
  ScheduledEvent,
  Team,
  Player,
  Championship,
  ChampionshipPoint,
)

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

  async def update_championship_standings(self):
    championship = await Championship.objects.alast()
    if not championship:
      return
    personal_standings = [s async for s in ChampionshipPoint.objects.personal_standings(championship)]
    team_standings = [s async for s in ChampionshipPoint.objects.team_standings(championship)]
    
    embed = discord.Embed(
      title=f"{championship.name} Standings",
      color=discord.Color.blue(),  # You can choose any color
    )
    team_standings_str = '\n'.join([
      f"{str(rank).rjust(2)}. {s['team__tag'].ljust(6)} {s['team__name'].ljust(30)} {str(s['total_points']).rjust(3)}"
      for rank, s in enumerate(team_standings, start=1)
    ])
    embed.add_field(
      name="Team Standings",
      value=f"```\n{team_standings_str}\n```",
      inline=False
    )
    personal_standings_str = '\n'.join([
      f"{str(rank).rjust(2)}. {s['character_name'].ljust(16)} {str(s['total_points']).rjust(3)}"
      for rank, s in enumerate(personal_standings, start=1)
    ])
    embed.add_field(
      name="Personal Standings",
      value=f"```\n{personal_standings_str}\n```",
      inline=False
    )

    last_embed_message = self.last_embed_message
    channel = self.bot.get_channel(settings.DISCORD_CHAMPIONSHIP_CHANNEL_ID)
    if last_embed_message is None:
        async for message in channel.history(limit=1):
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

