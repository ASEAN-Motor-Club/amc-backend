import sys
import asyncio
from arq import create_pool
from arq.connections import RedisSettings

async def _async_handle(*args, **options):
  redis = await create_pool(RedisSettings())

  for line in sys.stdin:
    _log_timestamp, hostname, tag, filename, game_timestamp, content = line.split(' ', 5)
    if tag == "necesse":
      await redis.enqueue_job('process_necesse_log', line)
    else:
      await redis.enqueue_job('process_log_line', line)
    sys.stdout.write("OK\n")

def main():
  asyncio.run(_async_handle())


