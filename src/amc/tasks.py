from django.db.utils import IntegrityError
from amc.models import ServerLog
from amc.server_logs import (
  parse_log_line,
  LogEvent,
  PlayerChatMessageLogEvent,
  PlayerVehicleLogEvent,
  PlayerCreatedCompanyLogEvent,
  PlayerLevelChangedLogEvent,
  PlayerLoginLogEvent,
  PlayerLogoutLogEvent,
  PlayerRestockedDepotLogEvent,
  CompanyAddedLogEvent,
  CompanyRemovedLogEvent,
  AnnouncementLogEvent,
  SecurityAlertLogEvent,
  UnknownLogEntry,
)
from amc.models import (
  Player, Character, PlayerChatLog, PlayerVehicleLog, Vehicle
)


async def aget_or_create_character(player_name, player_id):
  player, _ = await Player.objects.aget_or_create(unique_id=player_id)
  character, _ = await Character.objects.aget_or_create(player=player, name=player_name)
  return (character, player)

async def process_log_event(event: LogEvent):
  match event:
    case PlayerChatMessageLogEvent(timestamp, player_name, player_id, message):
      character, _ = await aget_or_create_character(player_name, player_id)
      await PlayerChatLog.objects.acreate(
        timestamp=timestamp,
        character=character, 
        text=message,
      )
    case PlayerVehicleLogEvent(timestamp, player_name, player_id, vehicle_name, vehicle_id):
      action = PlayerVehicleLog.action_for_event(event)
      character, _ = await aget_or_create_character(player_name, player_id)
      vehicle, _ = await Vehicle.objects.aget_or_create(id=vehicle_id, defaults={'name': vehicle_name})
      await PlayerVehicleLog.objects.acreate(
        timestamp=timestamp,
        character=character, 
        vehicle=vehicle,
        action=action,
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

