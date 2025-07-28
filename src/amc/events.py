import asyncio
import discord
from urllib.parse import quote
from django.conf import settings
from django.db.models import Prefetch, aprefetch_related_objects
from amc.mod_server import show_popup
from amc.models import (
  Character,
  GameEvent,
  GameEventCharacter,
  LapSectionTime,
  RaceSetup,
)

async def process_event(event):
  transition = None
  race_setup_hash = RaceSetup.calculate_hash(event['RaceSetup'])
  race_setup, _ = await RaceSetup.objects.aget_or_create(
    hash=race_setup_hash,
    defaults={
      'config': event['RaceSetup'],
      'name': event['RaceSetup'].get('Route', {}).get('RouteName')
    }
  )
  try:
    game_event = await (GameEvent.objects
      .filter(
        guid=event['EventGuid'],
        state__lte=event['State'],
      )
      .alatest('start_time')
    )

    if game_event.state != event['State']:
      transition = (game_event.state, event['State'])

    game_event.state = event['State']
    game_event.race_setup = race_setup
    await game_event.asave()
  except GameEvent.DoesNotExist:
    game_event = await GameEvent.objects.acreate(
      guid=event['EventGuid'],
      name=event['EventName'],
      state=event['State'],
      race_setup=race_setup,
    )

  async def process_player(player_info):
    character, *_ = await Character.objects.aget_or_create_character_player(
      player_info['PlayerName'],
      int(player_info['CharacterId']['UniqueNetId']),
    )
    player_finished = await GameEventCharacter.objects.filter(
      character=character,
      game_event=game_event,
      finished=True
    ).aexists()
    if player_finished:
      # Do not update finished players
      return

    defaults = {
      'last_section_total_time_seconds': player_info['LastSectionTotalTimeSeconds'],
      'section_index': player_info['SectionIndex'],
      'best_lap_time': player_info['BestLapTime'],
      'rank': player_info['Rank'],
      'laps': player_info['Laps'],
      'finished': player_info['bFinished'],
      'disqualified': player_info['bDisqualified'],
      'lap_times': list(player_info["LapTimes"]),
    }
    if game_event.state < 2:
      defaults = {
        **defaults,
        'wrong_vehicle': player_info['bWrongVehicle'],
        'wrong_engine': player_info['bWrongEngine'],
      }
    if player_info['SectionIndex'] == 0 and player_info['Laps'] == 1:
      defaults['first_section_total_time_seconds'] = player_info['LastSectionTotalTimeSeconds']

    game_event_character, _ = await GameEventCharacter.objects.aupdate_or_create(
      character=character,
      game_event=game_event,
      defaults=defaults,
      create_defaults={
        **defaults,
        'wrong_vehicle': player_info['bWrongVehicle'],
        'wrong_engine': player_info['bWrongEngine'],
      }
    )

    if game_event_character.section_index >= 0 and game_event_character.laps >= 1:
      laps = game_event_character.laps - 1
      section_index = game_event_character.section_index
      await LapSectionTime.objects.aget_or_create(
        game_event_character=game_event_character,
        section_index=section_index,
        lap=laps,
        defaults={
          'total_time_seconds': game_event_character.last_section_total_time_seconds,
          'rank': game_event_character.rank,
        }
      )

    return game_event_character

  await asyncio.gather(*[
    process_player(player_info)
    for player_info in event['Players']
  ])

  return game_event, transition

def format_time(total_seconds: float) -> str:
  if total_seconds is None:
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


def print_results(participants):
  lines = [
    f"#{str(rank).zfill(2)}: {participant.character.name.ljust(16)} {format_time(participant.net_time)}"
    for rank, participant in enumerate(participants, start=1)
  ]
  return '\n'.join(lines)


async def monitor_events(ctx):
  http_client = ctx.get('http_client_event_mod')
  async with http_client.get('/events') as resp:
    events = (await resp.json()).get('data', [])
    results = await asyncio.gather(*[
      process_event(event)
      for event in events
    ])
    for (game_event, transition) in results:
      if transition == (2, 3): # Finished
        await asyncio.sleep(1)
        participants = [p async for p in (GameEventCharacter.objects
          .select_related('character', 'character__player')
          .filter(
            game_event=game_event,
          )
        )]
        message = f"RESULTS\n\n{print_results(participants)}"
        await asyncio.gather(*[
          show_popup(http_client, message, player_id=participant.character.player.unique_id)
          for participant in participants
        ])



def create_event_embed(game_event):
  """Displays the event information in an embed."""

  race_setup = game_event.race_setup
  url = f"https://api.aseanmotorclub.com/race_setups/{race_setup.hash}/"
  track_editor_link = f"https://www.aseanmotorclub.com/track?uri={quote(url, safe='')}"
  embed = discord.Embed(
    title=f"🏁 Event: {game_event.name}",
    color=discord.Color.blue(),  # You can choose any color
    url=track_editor_link,
  )

  embed.add_field(name="🔀 Route", value=str(race_setup), inline=False)
  if race_setup.vehicles:
    embed.add_field(name="Vehicles", value=', '.join(race_setup.vehicles), inline=False)
  if race_setup.engines:
    embed.add_field(name="Engines", value=', '.join(race_setup.engines), inline=False)

  participant_list_str = ""
  for rank, participant in enumerate(game_event.participants.all(), start=1):
    try:
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
    except Exception as e:
      print(f"Failed to display participant: {e}")
      pass
  
      
  embed.add_field(name="👥 Participants", value=participant_list_str.strip(), inline=False)

  # You can add more fields from the 'event' dictionary if needed
  match game_event.state:
    case 1:
      state_str = 'Ready'
    case 2:
      state_str = 'In Progress'
    case 3:
      state_str = 'Finished'
    case 0:
      state_str = 'Not Ready'
  embed.set_footer(text=f"Status: {state_str}")

  return embed


async def send_event_embed(game_event, channel):
  embed = create_event_embed(game_event)

  ## Create embed
  if game_event.discord_message_id is None:
    message = await channel.send('', embed=embed)
    game_event.discord_message_id = message.id
    await game_event.asave(update_fields=['discord_message_id'])
  else:
    message = await channel.fetch_message(game_event.discord_message_id)
    await message.edit(content='', embed=embed)

async def send_event_embeds(ctx):
  http_client = ctx.get('http_client_event_mod')
  discord_client = ctx.get('discord_client')
  if not discord_client.is_ready():
    await discord_client.wait_until_ready()
  channel = discord_client.get_channel(settings.DISCORD_EVENTS_CHANNEL_ID)

  async with http_client.get('/events') as resp:
    events = (await resp.json()).get('data', [])
    event_guids = [event['EventGuid'] for event in events]
    qs = (GameEvent.objects
      .select_related('race_setup')
      .prefetch_related(
        Prefetch('participants', queryset=GameEventCharacter.objects.select_related('character'))
      )
      .filter(guid__in=event_guids)
    )

    async for game_event in qs:
      asyncio.run_coroutine_threadsafe(
        send_event_embed(game_event, channel),
        discord_client.loop
      )

