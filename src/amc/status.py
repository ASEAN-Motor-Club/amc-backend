import psutil
from amc.mod_server import get_status, set_config, list_player_vehicles
from amc.game_server import get_players, announce
from amc.models import ServerStatus

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
  target_fps = 26
  max_vehicles_per_player = min(
    base_vehicles_per_player,
    max(int(fps * base_vehicles_per_player * 20 / target_fps / len(players)), 3)
  ) - 1

  await set_config(ctx['http_client_mod'], max_vehicles_per_player)
  if fps < target_fps:
    if max_vehicles_per_player < base_vehicles_per_player:
      await announce(
        f"Max vehicles per player is now {max_vehicles_per_player}. Please despawn your unused vehicles to help server fps.",
        ctx['http_client'],
        color="FF0000"
      )

    for player_id, player_name in players:
      player_vehicles = await list_player_vehicles(ctx['http_client_mod'], player_id)
      if len(player_vehicles) > max_vehicles_per_player:
        await announce(
          f"{player_name}, please despawn your vehicles, you currently have {len(player_vehicles)} spanwed",
          ctx['http_client'],
        )

