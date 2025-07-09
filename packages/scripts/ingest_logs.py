import sys
import asyncio
from arq import create_pool
from arq.connections import RedisSettings

async def _async_handle(*args, **options):
  redis = await create_pool(RedisSettings())

  for line in sys.stdin:
    await redis.enqueue_job('process_log_line', line)
    sys.stdout.write("OK\n")

def main():
  asyncio.run(_async_handle())


