import re
from discord.ext import commands
from django.conf import settings

from amc.models import ScheduledEvent, Team, Player


class EventsCog(commands.Cog):
  def __init__(self, bot, teams_channel_id=settings.DISCORD_TEAMS_CHANNEL_ID):
    self.bot = bot
    self.teams_channel_id = teams_channel_id 

  @commands.Cog.listener()
  async def on_ready(self):
    await self.sync_teams()

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
      }
    )

  @commands.Cog.listener()
  async def on_scheduled_event_create(self, event):
    await ScheduledEvent.objects.acreate(
      discord_event_id=event.id,
      name=event.name,
      start_time=event.start_time,
      end_time=event.end_time,
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


