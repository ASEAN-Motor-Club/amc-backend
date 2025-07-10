from django.db.utils import IntegrityError
from django.db.models import F, Func, DateTimeField, ExpressionWrapper
from django.db.models.functions import Lower
from django.db.models.expressions import RawSQL
from django.db.backends.postgresql.psycopg_any import DateTimeRange
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
  Player,
  Character,
  PlayerStatusLog,
  PlayerChatLog,
  PlayerVehicleLog,
  Vehicle
)


async def aget_or_create_character(player_name, player_id):
  player, _ = await Player.objects.aget_or_create(unique_id=player_id)
  character, _ = await Character.objects.aget_or_create(player=player, name=player_name)
  return (character, player)

async def process_log_event(event: LogEvent, is_new_log_file: bool):
  if is_new_log_file:
    await PlayerStatusLog.objects.filter(timespan__upper_inf=True).aupdate(
      # can't find another way to update only the upper bound
      timespan=RawSQL("tstzrange( lower(timespan), %t )", (event.timestamp,))
    )

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
    case PlayerLoginLogEvent(timestamp, player_name, player_id):
      character, _ = await aget_or_create_character(player_name, player_id)
      await PlayerStatusLog.objects.filter(character=character, timespan__upper_inf=True).aupdate(
        # can't find another way to update only the upper bound
        timespan=RawSQL("tstzrange( lower(timespan), %t )", (timestamp,))
      )
      await PlayerStatusLog.objects.acreate(character=character, timespan=(timestamp, None))
    case PlayerLogoutLogEvent(timestamp, player_name, player_id):
      character, _ = await aget_or_create_character(player_name, player_id)
      await PlayerStatusLog.objects.filter(character=character, timespan__upper_inf=True).aupdate(
        # can't find another way to update only the upper bound
        timespan=RawSQL("tstzrange( lower(timespan), %t )", (timestamp,))
      )
    case _:
      raise ValueError('Unknown log')


async def process_log_line(ctx, line):
  log, event = parse_log_line(line)
  try:
    server_log = await ServerLog.objects.acreate(
      timestamp=log.timestamp,
      text=log.content,
      log_path=log.log_path,
    )
  except IntegrityError:
    return {'status': 'duplicate', 'timestamp': event.timestamp}

  is_new_log_file = await ServerLog.objects.filter(log_path=log.log_path).exclude(id=server_log.id).aexists()
  await process_log_event(event, is_new_log_file)

  return {'status': 'created', 'timestamp': event.timestamp}

