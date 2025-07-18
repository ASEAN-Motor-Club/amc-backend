import asyncio
from amc.models import (
  Character,
  GameEvent,
  GameEventCharacter,
  LapSectionTime,
  RaceSetup,
)

async def process_event(event):
  race_setup_hash = RaceSetup.calculate_hash(event['RaceSetup'])
  race_setup, _ = await RaceSetup.objects.aget_or_create(
    hash=race_setup_hash,
    defaults={
      'config': event['RaceSetup']
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

  await asyncio.gather(*[
    process_player(player_info)
    for player_info in event['Players']
  ])

async def monitor_events(ctx):
  http_client = ctx.get('http_client_mod')
  async with http_client.get('/events') as resp:
    events = (await resp.json()).get('data', [])
    await asyncio.gather(*[
      process_event(event)
      for event in events
    ])

