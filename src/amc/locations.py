import asyncio
from django.contrib.gis.geos import Point
from amc.models import Character, CharacterLocation
from amc.mod_server import show_popup

point_of_interests = [
  (
    Point(**{"z": -20696.78, "y": 150230.13, "x": 1025.73}),
    300,
    """\
[READ THE RULES]
Corporations are NOT ALLOWED - if you are planning to use AI drivers.
Too many corporation AI vehicles have caused traffic jams and other mishaps.

You may ONLY start a corporation for the following purposes:
- Spawning Campy's around the map
- Renting out vehicles for other players

For any other purposes, please contact the admins on the discord.
"""
  ),
]

async def process_player(player_info, ctx):
  character, player, *_  = await Character.objects.aget_or_create_character_player(
    player_info['PlayerName'],
    player_info['UniqueID'],
  )
  location_data = {
    axis.lower(): value
    for axis, value in player_info['Location'].items()
  }
  new_location_point = Point(**location_data)

  try:
    last_character_location = await CharacterLocation.objects.filter(
      character=character,
    ).alatest('timestamp')
  except CharacterLocation.DoesNotExist:
    last_character_location = None

  if last_character_location:
    for (target_point, target_radius_meters, message) in point_of_interests:
      distance_to_new = new_location_point.distance(target_point)
      distance_to_old = last_character_location.location.distance(target_point)

      was_outside = distance_to_old > target_radius_meters
      is_inside = distance_to_new <= target_radius_meters

      if was_outside and is_inside:
        asyncio.create_task(
          show_popup(ctx['http_client_mod'], message, player_id=player.unique_id)
        )

  await CharacterLocation.objects.acreate(
    character=character,
    location=new_location_point
  )


async def monitor_locations(ctx):
  http_client = ctx.get('http_client_mod')
  async with http_client.get('/players') as resp:
    players = (await resp.json()).get('data', [])
    await asyncio.gather(*[
      process_player(player, ctx)
      for player in players
    ])

