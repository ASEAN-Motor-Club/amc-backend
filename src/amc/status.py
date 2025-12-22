import psutil
from amc.mod_server import get_status, set_config, list_player_vehicles, teleport_player
from amc.game_server import get_players, announce
from amc.models import ServerStatus, CharacterLocation

async def monitor_server_status(ctx):
  status = await get_status(ctx['http_client_mod'])
  try:
    players = await get_players(ctx['http_client'])
  except Exception as e:
    print(f"Failed to get players: {e}")
    players = []

  mem = psutil.virtual_memory()
  await ServerStatus.objects.acreate(
    fps=status.get('FPS', 0),
    used_memory=mem.used,
    num_players=len(players)
  )

async def monitor_server_condition(ctx):
  status = await get_status(ctx['http_client_mod'])
  try:
    players = await get_players(ctx['http_client'])
  except Exception as e:
    print(f"Failed to get players: {e}")
    players = []

  fps = status.get('FPS')
  base_vehicles_per_player = 12
  target_fps = 22
  max_vehicles_per_player = min(
    base_vehicles_per_player,
    max(int(fps * base_vehicles_per_player * 20 / target_fps / len(players)), 3)
  ) - 1

  await set_config(ctx['http_client_mod'], max_vehicles_per_player)
  if fps < target_fps:
    if max_vehicles_per_player < base_vehicles_per_player:
      await announce(
        f"Max vehicles per player is now {max_vehicles_per_player}.",
        ctx['http_client'],
        color="FF59EE"
      )

  for player_id, player in players:
    player_vehicles = await list_player_vehicles(ctx['http_client_mod'], player_id)
    player_name = player.get('name')

    if fps < target_fps:
      if len(player_vehicles) > max_vehicles_per_player:
        await announce(
          f"{player_name}, please despawn your vehicles, you currently have {len(player_vehicles)} spanwed",
          ctx['http_client'],
        )

async def monitor_rp_mode(ctx):
  try:
    players = await get_players(ctx['http_client'])
  except Exception as e:
    print(f"Failed to get players: {e}")
    players = []

  for player_id, player in players:
    player_name = player.get('name')
    is_rp_mode = '[RP]' in player_name
    if not is_rp_mode:
      continue

    player_vehicles = await list_player_vehicles(ctx['http_client_mod'], player_id, active=True)
    if not player_vehicles:
      continue

    def is_position_zero(position):
      if not position:
        return True
      return position['X'] == 0 and position['Y'] == 0 and position['Z'] == 0

    is_autopilot = any([v.get('isLastVehicle') and v.get('bIsAIDriving') and not is_position_zero(v.get('position')) for v in player_vehicles.values()])
    if is_autopilot:
      character_location = await (CharacterLocation.objects
        .filter(character__guid=player.get('character_guid'))
        .alatest('timestamp')
      )
      await teleport_player(
        ctx['http_client_mod'],
        player_id,
        {
          'X': character_location.location.x,
          'Y': character_location.location.y,
          'Z': character_location.location.z,
        },
        no_vehicles=False,
        reset_trailers=False,
        reset_carried_vehicles=False,
      )
      await announce(
        f"{player_name}, you may not use Autopilot on RP mode",
        ctx['http_client'],
        color="FFA500"
      )


