import asyncio
from django.contrib.gis.geos import Point
from amc.models import Character, CharacterLocation

async def process_player(player_info):
  character, *_  = await Character.objects.aget_or_create_character_player(
    player_info['PlayerName'],
    player_info['UniqueID'],
  )
  location = {
    axis.lower(): value
    for axis, value in player_info['Location'].items()
  }
  await CharacterLocation.objects.acreate(
    character=character,
    location=Point(**location)
  )


async def monitor_locations(ctx):
  http_client = ctx.get('http_client_mod')
  async with http_client.get('/players') as resp:
    players = (await resp.json()).get('data', [])
    await asyncio.gather(*[
      process_player(player)
      for player in players
    ])

