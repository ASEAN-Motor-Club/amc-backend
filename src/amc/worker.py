from arq.connections import RedisSettings
import django
django.setup()
from django.conf import settings
from amc.tasks import process_log_line

REDIS_SETTINGS = RedisSettings(**settings.REDIS_SETTINGS)

async def startup(ctx):
  pass

async def shutdown(ctx):
  pass

class WorkerSettings:
    functions = [process_log_line]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = REDIS_SETTINGS
    max_jobs = 1 # to prevent race condition

