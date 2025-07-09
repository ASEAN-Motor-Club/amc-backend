from arq.connections import RedisSettings
import django
django.setup()
from django.db.utils import IntegrityError
from amc.models import ServerLog
from amc.server_logs import (
  parse_log_line,
  LogEvent,
  PlayerChatMessageLogEvent,
  PlayerCreatedCompanyLogEvent,
  PlayerLevelChangedLogEvent,
  PlayerLoginLogEvent,
  PlayerLogoutLogEvent,
  PlayerEnteredVehicleLogEvent,
  PlayerExitedVehicleLogEvent,
  PlayerBoughtVehicleLogEvent,
  PlayerSoldVehicleLogEvent,
  PlayerRestockedDepotLogEvent,
  CompanyAddedLogEvent,
  CompanyRemovedLogEvent,
  AnnouncementLogEvent,
  SecurityAlertLogEvent,
  UnknownLogEntry,
)
from django.conf import settings

REDIS_SETTINGS = RedisSettings(**settings.REDIS_SETTINGS)

async def process_log_line(ctx, line):
  event: LogEvent = parse_log_line(line)
  try:
    await ServerLog.objects.acreate(
      timestamp=event.timestamp,
      text=line
    )
  except IntegrityError:
    return {'status': 'duplicate', 'timestamp': event.timestamp}

  match event:
    case PlayerLoginLogEvent():
      pass
  return {'status': 'created', 'timestamp': event.timestamp}

async def startup(ctx):
  pass

async def shutdown(ctx):
  pass

class WorkerSettings:
    functions = [process_log_line]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = REDIS_SETTINGS

