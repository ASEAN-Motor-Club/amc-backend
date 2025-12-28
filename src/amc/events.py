import math
import asyncio
import discord
import aiohttp
from datetime import timedelta
from django.utils import timezone
from urllib.parse import quote
from django.conf import settings
from django.db.models import F, Prefetch, Exists, OuterRef, Window
from django.db.models.functions import RowNumber
from amc.mod_server import show_popup, send_system_message, teleport_player
from amc.game_server import announce
from amc.models import (
  Character,
  GameEvent,
  GameEventCharacter,
  LapSectionTime,
  RaceSetup,
  ScheduledEvent,
)

import uuid

def generate_guid():
  """
  Generates a random GUID (UUID version 4).

  Returns:
      str: The generated GUID as a string.
  """
  return str(uuid.uuid4()).replace('-', '').upper()

async def setup_event(timestamp, player_id, scheduled_event, http_client_mod):
  async with http_client_mod.get('/events') as resp:
    events = (await resp.json()).get('data', [])
    for event in events:
      if event['OwnerCharacterId']['UniqueNetId'] == str(player_id):
        raise Exception('You already have an active event')

  async with http_client_mod.get(f'/players/{player_id}') as resp:
    players = (await resp.json()).get('data', [])
    if not players:
      raise Exception('Player not found')
    player = players[0]

  race_setup = scheduled_event.race_setup.config
  race_setup['Route']['Waypoints'] = [
    {
      'Translation': waypoint['Location'],
      'Scale3D': waypoint['Scale3D'],
      'Rotation': waypoint['Rotation'],
    }
    for waypoint in race_setup['Route']['Waypoints']
  ]
  if len(race_setup['VehicleKeys']) == 0:
    race_setup['VehicleKeys'] = []
  if len(race_setup['EngineKeys']) == 0:
    race_setup['EngineKeys'] = []

  data = {
    'EventGuid': generate_guid(),
    'EventName': scheduled_event.name,
    'RaceSetup': race_setup,
    'EventType': 1,
    'OwnerCharacterId': {
      'CharacterGuid': player['CharacterGuid'].rjust(32, '0'),
      'UniqueNetId': str(player_id),
    }
  }
  async with http_client_mod.post('/events', json=data) as response:
    if response.status >= 400:
      error_body = await response.json()
      raise Exception(f"API Error: Received status {response.status} instead of 201. Body: {error_body}")
    return True


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
  owner = await Character.objects.filter(
    player__unique_id=event['OwnerCharacterId']['UniqueNetId'],
    guid=event['OwnerCharacterId']['CharacterGuid']
  ).afirst()

  scheduled_event = await ScheduledEvent.objects.filter(
    race_setup=race_setup,
    start_time__lte=timezone.now(),
    end_time__gte=timezone.now(),
    time_trial=True # only auto-associate time trials
  ).afirst()

  try:
    game_event = await (GameEvent.objects
      .filter(
        guid=event['EventGuid'],
        state__lte=event['State'],
      )
      .select_related('scheduled_event')
      .alatest('start_time')
    )

    if game_event.state != event['State']:
      transition = (game_event.state, event['State'])

    game_event.state = event['State']
    game_event.owner = owner
    game_event.race_setup = race_setup
    if not game_event.scheduled_event:
      game_event.scheduled_event = scheduled_event
    await game_event.asave()
  except GameEvent.DoesNotExist:
    try:
      # TODO: Refactor, use the above query as the existing_event
      existing_event = await (GameEvent.objects
        .filter(
          guid=event['EventGuid'],
          discord_message_id__isnull=False,
        )
        .exclude(Exists(
          GameEventCharacter.objects.filter(
            game_event=OuterRef('pk'),
            finished=True
          )
        ))
        .alatest('last_updated')
      )
      discord_message_id = existing_event.discord_message_id
    except GameEvent.DoesNotExist:
      discord_message_id = None

    game_event = await GameEvent.objects.acreate(
      guid=event['EventGuid'],
      name=event['EventName'],
      state=event['State'],
      race_setup=race_setup,
      discord_message_id=discord_message_id,
      owner=owner,
      scheduled_event=scheduled_event,
    )

  async def process_player(player_info):
    character, *_ = await Character.objects.aget_or_create_character_player(
      player_info['PlayerName'],
      int(player_info['CharacterId']['UniqueNetId']),
      character_guid=player_info['CharacterId']['CharacterGuid'],
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
    if game_event.state == 2 and player_info['SectionIndex'] == 0 and player_info['Laps'] == 1:
      # There's a bug where the first section is a big number
      if player_info['LastSectionTotalTimeSeconds'] < 10_000_000:
        defaults['first_section_total_time_seconds'] = player_info['LastSectionTotalTimeSeconds']
      else:
        defaults['first_section_total_time_seconds'] = 0

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

    if game_event.state >= 2 and game_event_character.section_index >= 0 and game_event_character.laps >= 1:
      laps = game_event_character.laps - 1
      section_index = game_event_character.section_index
      await LapSectionTime.objects.aupdate_or_create(
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

  return game_event, transition, scheduled_event

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


def print_results(participants):
  def print_result(participant, rank):
    flags = []
    if not participant.finished:
      flags.append('DNF')
    if participant.wrong_engine:
      flags.append('ENGINE')
    if participant.wrong_vehicle:
      flags.append('VEHICLE')

    flags = ', '.join(flags)
    return f"#{str(rank).zfill(2)}: <Bold>{participant.character.name.ljust(16)}</> {format_time(participant.net_time).ljust(14)} <Warning>{flags}</>"

  lines = [
    print_result(participant, rank)
    for rank, participant in enumerate(participants, start=1)
  ]
  return '\n'.join(lines)


async def show_results_popup(http_client, participants, player_id=None, character_guid=None):
  message = f"<Title>Results</>\n\n{print_results(participants)}"
  if player_id is not None or character_guid is not None:
    await show_popup(http_client, message, player_id=player_id, character_guid=character_guid)
    return


  for participant in participants:
    await show_popup(
      http_client,
      message,
      character_guid=participant.character.guid,
    )


async def show_scheduled_event_results_popup(http_client, scheduled_event, player_id=None, character_guid=None):
  participants = [
    p
    async for p in GameEventCharacter.objects.results_for_scheduled_event(scheduled_event)
  ]
  await show_results_popup(http_client, participants, player_id=player_id, character_guid=character_guid)


async def monitor_events(ctx, http_client):
  discord_client = ctx.get('discord_client')
  events_cog = discord_client.get_cog('EventsCog')

  try:
    async with http_client.get('/events') as resp:
      events = (await resp.json()).get('data', [])
      results = await asyncio.gather(*[
        process_event(event)
        for event in events
      ])

      for (game_event, transition, scheduled_event) in results:
        if transition == (2, 3): # Finished
          participants = [p async for p in (GameEventCharacter.objects
            .select_related('character', 'character__player')
            .filter(
              game_event=game_event,
            )
          )]
          await show_results_popup(http_client, participants)
          try:
            if scheduled_event and events_cog and hasattr(events_cog, 'update_scheduled_event_embed'):
              loop = asyncio.get_running_loop()
              loop.run_in_executor(
                None,
                lambda: asyncio.run_coroutine_threadsafe(
                  events_cog.update_scheduled_event_embed(
                    scheduled_event.id
                  ),
                  discord_client.loop
                )
              )
          except Exception as e:
            print(f"Failed to update scheduled event embed: {e}")

  except Exception:
    pass



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

  if game_event.scheduled_event is not None:
    embed.add_field(name="🕒 Results", value=f"https://www.aseanmotorclub.com/championship?event={game_event.scheduled_event.id}", inline=False)

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
    case _:
      state_str = 'Unknown'
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
    try:
      message = await channel.fetch_message(game_event.discord_message_id)
      await message.edit(content='', embed=embed)
    except discord.NotFound:
      message = await channel.send('', embed=embed)
      game_event.discord_message_id = message.id
      await game_event.asave(update_fields=['discord_message_id'])

async def send_event_embeds(ctx):
  http_client = ctx.get('http_client_event_mod')
  discord_client = ctx.get('discord_client')
  if not discord_client.is_ready():
    await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(
      discord_client.wait_until_ready(),
      discord_client.loop
    )) 
  channel = discord_client.get_channel(settings.DISCORD_EVENTS_CHANNEL_ID)

  try: 
    async with http_client.get('/events') as resp:
      if resp.status != 200:
        return
      events = (await resp.json()).get('data', [])
  except aiohttp.ClientConnectorError:
    return

  event_guids = [event['EventGuid'] for event in events]
  qs = (GameEvent.objects
    .select_related('race_setup', 'scheduled_event')
    .prefetch_related(
      Prefetch('participants', queryset=GameEventCharacter.objects.select_related('character'))
    )
    .annotate(
      rank=Window(
        expression=RowNumber(),
        partition_by=[F('guid')],
        order_by=[F('last_updated').desc()]
      )
    )
    .filter(rank=1, guid__in=event_guids)
  )

  async for game_event in qs:
    asyncio.run_coroutine_threadsafe(
      send_event_embed(game_event, channel),
      discord_client.loop
    )

  # Remove expired embeds

  expired_discord_message_ids = list(set([
    discord_message_id
    async for discord_message_id in (GameEvent.objects
      .filter(discord_message_id__isnull=False, last_updated__gte=timezone.now() - timedelta(days=7))
      .exclude(Exists(
        GameEventCharacter.objects.filter(
          game_event=OuterRef('pk'),
          finished=True
        )
      ))
      .difference(qs)
      .order_by('-last_updated')
      .values_list('discord_message_id', flat=True)
    )[:50]
  ]))
  async def delete_expired_messages(mIds):
    expired_discord_messages = [discord.Object(id=str(mId)) for mId in mIds]
    if expired_discord_messages:
      try:
        await GameEvent.objects.filter(discord_message_id__in=mIds).aupdate(discord_message_id=None)
        await channel.delete_messages(expired_discord_messages)
      except Exception as e:
        print(f'Failed to delete {mIds}: {e}', flush=True)

  async def delete_unattached_embeds():
    to_delete = []
    async for m in channel.history(limit=20):
      if not (await GameEvent.objects.filter(discord_message_id=m.id).aexists()):
        to_delete.append(m)
    await channel.delete_messages(to_delete)

  asyncio.run_coroutine_threadsafe(
    delete_expired_messages(expired_discord_message_ids),
    discord_client.loop
  )
  asyncio.run_coroutine_threadsafe(
    delete_unattached_embeds(),
    discord_client.loop
  )

async def staggered_start(http_client_game, http_client_mod, game_event, player_id=None, delay=20.0):
  async with http_client_mod.get(f'/events/{game_event.guid}') as resp:
    events = (await resp.json()).get('data', [])

  if not events:
    raise Exception('Event not found')
  event = events[0]

  if event['State'] != 1:
    raise Exception('Event is not in Ready state')

  participants = [
    player_info
    for player_info in event['Players']
  ]
  line_up_message = f'<Title>Staggered Start Line Up</>\n\nThe event will start in 30 seconds!\nYour time will only be counted when you cross the starting line\n<Secondary>Delay between participants = {delay} seconds</>\n<Announce>ONLY start when your name is called!!</>\n\n'
  line_up_message += '\n'.join([
    f"{idx}. {player_info['PlayerName']}"
    for idx, player_info in enumerate(participants, start=1)
  ])
  for player_info in participants:
    await show_popup(
      http_client_mod,
      line_up_message,
      player_id=player_info['CharacterId']['UniqueNetId']
    )

  await announce(
    "The event is starting in 30 seconds!",
    http_client_game,
  )
  await asyncio.sleep(30.0) # in-game countdown

  await announce(
    "The event is starting starting!",
    http_client_game,
  )

  await http_client_mod.post(f"/events/{event['EventGuid']}/state", json={
    "State": 2,
  })

  await asyncio.sleep(5.0) # in-game countdown

  for player_info in participants:
    await asyncio.sleep(delay)
    await asyncio.gather(
      announce(
        f"{player_info['PlayerName']} GO!!!",
        http_client_game,
      ),
      send_system_message(
        http_client_mod,
        "GO!!!",
        character_guid=player_info['CharacterId']['CharacterGuid']
      )
    )

def _rotate_vector_by_quaternion(vector, quat):
    """
    Rotates a 3D vector by a quaternion.

    Args:
        vector (dict): The vector to rotate {'x': float, 'y': float, 'z': float}.
        quat (dict): The quaternion for rotation {'w': float, 'x': float, 'y': float, 'z': float}.

    Returns:
        dict: The rotated vector.
    """
    # Normalize the quaternion to be safe
    q_mag = math.sqrt(quat['W']**2 + quat['X']**2 + quat['Y']**2 + quat['Z']**2)
    if q_mag == 0:
      return vector # Avoid division by zero
    qw, qx, qy, qz = quat['W']/q_mag, quat['X']/q_mag, quat['Y']/q_mag, quat['Z']/q_mag
    
    # Hamilton product: q * v * q_conjugate
    # First, q * v
    w_res = -qx * vector['X'] - qy * vector['Y'] - qz * vector['Z']
    x_res =  qw * vector['X'] + qy * vector['Z'] - qz * vector['Y']
    y_res =  qw * vector['Y'] - qx * vector['Z'] + qz * vector['X']
    z_res =  qw * vector['Z'] + qx * vector['Y'] - qy * vector['X']

    # Then, (q * v) * q_conjugate
    final_x = w_res * -qx + x_res * qw + y_res * -qz - z_res * -qy
    final_y = w_res * -qy - x_res * -qz + y_res * qw + z_res * -qx
    final_z = w_res * -qz + x_res * -qy - y_res * -qx + z_res * qw
    
    return {'X': final_x, 'Y': final_y, 'Z': final_z}


async def auto_starting_grid(http_client_mod, game_event):
  async with http_client_mod.get(f'/events/{game_event.guid}') as resp:
    events = (await resp.json()).get('data', [])

  if not events:
    raise Exception('Event not found')
  event = events[0]

  if event['State'] != 1:
    raise Exception('Event is not in Ready state')

  participants = [
    player_info
    for player_info in event['Players']
  ]

  config = {
    'lateral_spacing': game_event.race_setup.lateral_spacing,
    'longitudinal_spacing': game_event.race_setup.longitudinal_spacing,
    'initial_offset': game_event.race_setup.initial_offset,
    'pole_side': "right" if game_event.race_setup.pole_side_right else "left",
    'reverse_starting_direction': game_event.race_setup.reverse_starting_direction,
  }
  starting_point = game_event.race_setup.waypoints[0]
  start_pos = starting_point['Location']
  start_quat = starting_point['Rotation']
  lateral_spacing = int(config.get('lateral_spacing', 600))
  longitudinal_spacing = int(config.get('longitudinal_spacing', 1000))
  initial_offset = int(config.get('initial_offset', 1000))
  pole_side = str(config.get('pole_side', 'right'))

  # --- 2. Vector Calculations from Quaternion ---
  # Define base vectors in a standard coordinate system (e.g., X-Forward, Y-Left, Z-Up)
  base_forward = {'X': -1, 'Y': 0, 'Z': 0}
  base_right = {'X': 0, 'Y': -1, 'Z': 0} # Negative Y is right if positive Y is left

  # Rotate these base vectors by the start line's quaternion to get world-space directions
  forward_vec = _rotate_vector_by_quaternion(base_forward, start_quat)
  right_vec = _rotate_vector_by_quaternion(base_right, start_quat)
  
  # For the output, calculate the effective yaw from the new forward vector
  yaw_deg = math.degrees(math.atan2(forward_vec['Y'], forward_vec['X']))
  if config.get('reverse_starting_direction', False):
    yaw_deg += 180

  pole_side_multiplier = 1 if pole_side == 'right' else -1

  for i, player_info in enumerate(participants):
    row = i // 2
    side = 1 if i % 2 == 0 else -1

    # a) Longitudinal offset (how far back from the line)
    total_longitudinal_offset = initial_offset + (row * longitudinal_spacing)
    longitudinal_displacement = {
      'X': -total_longitudinal_offset * forward_vec['X'],
      'Y': -total_longitudinal_offset * forward_vec['Y'],
      'Z': -total_longitudinal_offset * forward_vec['Z']
    }

    # b) Lateral offset (how far to the side of the line)
    total_lateral_offset = side * pole_side_multiplier * (lateral_spacing / 2)
    lateral_displacement = {
      'X': total_lateral_offset * right_vec['X'],
      'Y': total_lateral_offset * right_vec['Y'],
      'Z': total_lateral_offset * right_vec['Z'] # Account for roll
    }

    # --- 4. Final Position Calculation ---
    final_x = start_pos['X'] + longitudinal_displacement['X'] + lateral_displacement['X']
    final_y = start_pos['Y'] + longitudinal_displacement['Y'] + lateral_displacement['Y']
    final_z = start_pos['Z'] + longitudinal_displacement['Z'] + lateral_displacement['Z']

    player_location = {
      'X': final_x,
      'Y': final_y,
      'Z': final_z + 20,
    }
    player_rotation = {
      'Roll': 0,
      'Pitch': 0,
      'Yaw': yaw_deg
    }
    await asyncio.sleep(0.2)
    await teleport_player(
      http_client_mod,
      player_info['CharacterId']['UniqueNetId'],
      player_location,
      player_rotation
    )

