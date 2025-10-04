import psutil
from amc.mod_server import get_status
from amc.models import ServerStatus

async def monitor_server_status(ctx):
  session = ctx['http_client_mod']
  status = await get_status(session)
  mem = psutil.virtual_memory()
  await ServerStatus.objects.acreate(fps=status.get('FPS', 0), used_memory=mem.used)

