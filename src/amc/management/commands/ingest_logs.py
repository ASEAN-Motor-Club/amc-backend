import sys
import asyncio
from django.core.management.base import BaseCommand
from arq import create_pool
from arq.connections import RedisSettings
from django.conf import settings

class Command(BaseCommand):
  help = "Ingest game logs"

  async def _async_handle(self, *args, **options):
    redis = await create_pool(RedisSettings(**settings.REDIS_SETTINGS))

    for line in sys.stdin:
      await redis.enqueue_job('process_log_line', line)
      self.stdout.write("OK")

  def handle(self, *args, **options):
      asyncio.run(self._async_handle(*args, **options))

