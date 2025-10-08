import psutil
from amc.mod_server import get_status
from amc.game_server import get_players
from amc.models import ServerStatus

async def monitor_server_status(ctx):
  status = await get_status(ctx['http_client_mod'])
  try:
    players = await get_players(ctx['http_client_game'])
  except Exception:
    players = []

  mem = psutil.virtual_memory()
  await ServerStatus.objects.acreate(
    fps=status.get('FPS', 0),
    used_memory=mem.used,
    num_players=len(players)
  )

