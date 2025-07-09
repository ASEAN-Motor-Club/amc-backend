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
from amc.models import Player, Character, PlayerChatLog


async def process_log_event(event: LogEvent):
  match event:
    case PlayerLoginLogEvent():
      pass
    case PlayerChatMessageLogEvent(timestamp, player_name, player_id, message):
      player, _ = await Player.objects.aget_or_create(unique_id=player_id)
      character, _ = await Character.objects.aget_or_create(player=player, name=player_name)
      await PlayerChatLog.objects.acreate(
        timestamp=timestamp,
        character=character, 
        text=message,
      )
    case _:
      raise ValueError('Unknown log')


async def process_log_line(ctx, line):
  event: LogEvent = parse_log_line(line)
  try:
    await ServerLog.objects.acreate(
      timestamp=event.timestamp,
      text=line
    )
  except IntegrityError:
    return {'status': 'duplicate', 'timestamp': event.timestamp}

  try:
    await process_log_event(event)
  except ValueError as e:
    return {'status': 'error', 'timestamp': event.timestamp, 'error': str(e)}

  return {'status': 'created', 'timestamp': event.timestamp}

