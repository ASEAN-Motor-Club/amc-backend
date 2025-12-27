import asyncio
from django.contrib.gis.geos import Point
from amc.models import Character, CharacterLocation
from amc.mod_server import show_popup, teleport_player
from django.conf import settings

gwangjin_shortcut = Point(359285, 892222, -3519).buffer(100_00)
migeum_shortcut = Point(227878, 449541, -9308).buffer(60_00)
point_of_interests = [
  (
    Point(**{"z": -20696.78, "y": 150230.13, "x": 1025.73}),
    300,
    """\
<Title>Corporation Rules</>
<Warning>Corporations are NOT ALLOWED</> - if you are planning to use AI drivers.
Having too many AI vehicles on the server have caused traffic jams and other mishaps.
Unlicensed corporations will be closed down!

<Bold>You may ONLY start a corporation for the following purposes:</>
- Spawning Campy's around the map
- Renting out vehicles for other players
- Showcasing liveries for car shows

For any other purposes, <Highlight>please contact the admins on the discord</>.
"""
  ),
  (
    Point(**{"x": -220383.08, "y": 141777.71, "z": -20186.82}),
    500,
    """\
<Title>Want to take out a loan?</>
<Warning>This bank charges high interest rate!</> - many players have ended up in a debt spiral.

<Bold>Use Bank ASEAN instead!</>
- Our loans are interest free
- Our loans have to repayment period
- You only have to pay them back when you make a profit

Use <Highlight>/bank</> to create a Bank ASEAN account today!
"""
  ),
  (
    Point(** {"z": -21564.73, "y": 157275.61, "x": -83784.37}),
    2000,
    f"""\
<Title>Welcome to the ASEAN Park</>

{settings.CREDITS_TEXT}
"""
  ),
]

portals = [
  # Meehoi house
  (
    Point(**{"x": 69664.27, "y": 651361.93, "z": -8214.26}),
    150,
    Point(**{"x": 68205.77, "y": 651084.19, "z": -7000.43}),
  ),
  (
    Point(**{"x": 68119.18, "y": 650502.15, "z": -6909.83}),
    120,
    Point(**{"x": 67912.23, "y": 650236.37, "z": -8512.19}),
  ),

  # Rooftop Bar
  (
    Point(**{ "x": -67173.12, "y": 150561.7, "z": -20646.4 } ),
    150,
    Point(**{ "x": -66531.100038674, "y": 150471.72884842, "z": -19706.865 } ),
  ),
  (
    Point(**{ "x": -66733.74, "y": 150411.51, "z": -19703.15 } ),
    120,
    Point(**{ "x": -67245.74, "y": 150831.6, "z": -20646.85 } ),
  ),
]
async def process_player(player_info, ctx):
  try:
    character = await Character.objects.select_related('player').aget(
      name=player_info['PlayerName'],
      guid=player_info['CharacterGuid'],
    )
  except Character.DoesNotExist:
    return
  player = character.player

  location_data = {
    axis.lower(): value
    for axis, value in player_info['Location'].items()
  }
  new_location_point = Point(**location_data)
  vehicle_key = player_info['VehicleKey']

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

      if was_outside and is_inside and ctx.get('http_client_mod') is not None:
        await show_popup(ctx['http_client_mod'], message, player_id=player.unique_id)
        await asyncio.sleep(0.1)

    for (source_point, source_radius_meters, target_point) in portals:
      distance_to_new = new_location_point.distance(source_point)
      distance_to_old = last_character_location.location.distance(source_point)

      was_outside = distance_to_old > source_radius_meters
      is_inside = distance_to_new <= source_radius_meters

      if was_outside and is_inside and ctx.get('http_client_mod') is not None:
        await teleport_player(
          ctx['http_client_mod'],
          str(player.unique_id),
          {'X': target_point.x, 'Y': target_point.y, 'Z': target_point.z},
        )
        await asyncio.sleep(0.1)

  await CharacterLocation.objects.acreate(
    character=character,
    location=new_location_point,
    vehicle_key=vehicle_key,
  )


async def monitor_locations(ctx):
  http_client = ctx.get('http_client_mod')
  async with http_client.get('/players') as resp:
    players = (await resp.json()).get('data', [])
    await asyncio.gather(*[
      process_player(player, ctx)
      for player in players
    ])

