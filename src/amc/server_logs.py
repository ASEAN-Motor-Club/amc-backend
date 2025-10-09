import re
from abc import ABC, ABCMeta
from dataclasses import dataclass
from datetime import datetime
from django.utils import timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class BaseLogEvent(ABC):
  """An abstract base class for any log event."""
  timestamp: datetime

@dataclass(frozen=True)
class PlayerChatMessageLogEvent(BaseLogEvent):
  """Represents a message sent by a player in the game chat."""
  player_name: str
  player_id: int
  message: str

@dataclass(frozen=True)
class PlayerCreatedCompanyLogEvent(BaseLogEvent):
  """Represents a message sent by a player in the game chat."""
  player_name: str
  company_name: str

@dataclass(frozen=True)
class PlayerLevelChangedLogEvent(BaseLogEvent):
  """Represents a message sent by a player in the game chat."""
  player_name: str
  player_id: int
  level_type: str
  level_value: int

@dataclass(frozen=True)
class PlayerLoginLogEvent(BaseLogEvent):
  """Represents a player successfully logging into the server."""
  player_name: str
  player_id: int

@dataclass(frozen=True)
class PlayerLogoutLogEvent(BaseLogEvent):
  """Represents a player logging out."""
  player_name: str
  player_id: int

@dataclass(frozen=True)
class LegacyPlayerLogoutLogEvent(BaseLogEvent):
  """(Legacy) Represents a player logging out. Missing player_id"""
  player_name: str

@dataclass(frozen=True)
class PlayerVehicleLogEvent(BaseLogEvent, metaclass=ABCMeta):
  """Represents a player logging out."""
  player_name: str
  player_id: int
  vehicle_name: str
  vehicle_id: int

@dataclass(frozen=True)
class PlayerEnteredVehicleLogEvent(PlayerVehicleLogEvent):
  """Represents a player logging out."""
  pass

@dataclass(frozen=True)
class PlayerExitedVehicleLogEvent(PlayerVehicleLogEvent):
  """Represents a player logging out."""
  pass

@dataclass(frozen=True)
class PlayerBoughtVehicleLogEvent(PlayerVehicleLogEvent):
  """Represents a player logging out."""
  pass

@dataclass(frozen=True)
class PlayerSoldVehicleLogEvent(PlayerVehicleLogEvent):
  """Represents a player logging out."""
  pass

@dataclass(frozen=True)
class PlayerRestockedDepotLogEvent(BaseLogEvent):
  """Represents a player logging out."""
  player_name: str
  depot_name: str

@dataclass(frozen=True)
class CompanyAddedLogEvent(BaseLogEvent):
  """Represents a player logging out."""
  company_name: str
  is_corp: bool
  owner_name: str
  owner_id: int

@dataclass(frozen=True)
class CompanyRemovedLogEvent(BaseLogEvent):
  """Represents a player logging out."""
  company_name: str
  is_corp: bool
  owner_name: str
  owner_id: int

@dataclass(frozen=True)
class AnnouncementLogEvent(BaseLogEvent):
  """Represents a player logging out."""
  message: str

@dataclass(frozen=True)
class ServerStartedLogEvent(BaseLogEvent):
  """Represents a player logging out."""
  version: str

@dataclass(frozen=True)
class SecurityAlertLogEvent(BaseLogEvent):
  """Represents a player logging out."""
  player_name: str
  player_id: int
  message: str

@dataclass(frozen=True)
class UnknownLogEntry(BaseLogEvent):
  """Represents a log line that could not be parsed into a known event."""
  original_line: str

@dataclass(frozen=True)
class ServerLog(BaseLogEvent):
  """Represents line of log."""
  content: str
  log_path: str
  hostname: str
  tag: str


LogEvent = (
  PlayerChatMessageLogEvent
  | PlayerCreatedCompanyLogEvent
  | PlayerLevelChangedLogEvent
  | PlayerLoginLogEvent
  | PlayerLogoutLogEvent
  | LegacyPlayerLogoutLogEvent
  | PlayerEnteredVehicleLogEvent
  | PlayerExitedVehicleLogEvent
  | PlayerBoughtVehicleLogEvent
  | PlayerSoldVehicleLogEvent
  | PlayerRestockedDepotLogEvent
  | CompanyAddedLogEvent
  | CompanyRemovedLogEvent
  | AnnouncementLogEvent
  | SecurityAlertLogEvent
  | UnknownLogEntry
)

GAME_TIMESTAMP_FORMAT = '%Y.%m.%d-%H.%M.%S'

def parse_log_line(line: str) -> tuple[ServerLog, LogEvent]:
  try:
    _log_timestamp, hostname, tag, filename, game_timestamp, content = line.split(' ', 5)
    timestamp = datetime.strptime(game_timestamp.strip('[').strip(']'), GAME_TIMESTAMP_FORMAT).replace(tzinfo=ZoneInfo('Asia/Bangkok'))
    server_log = ServerLog(
      timestamp=timestamp,
      content=content,
      log_path=filename,
      hostname=hostname,
      tag=tag,
    )
  except ValueError:
    return ServerLog(timestamp=timezone.now(), content=line, log_path="", hostname="", tag=""), UnknownLogEntry(timestamp=timezone.now(), original_line=line)
  return server_log, parse_log_content(timestamp, content)

def parse_log_content(timestamp, content):
  if pattern_match := re.match(r"\[CHAT\] (?P<player_name>.+) \((?P<player_id>\d+)\): (?P<message>.+)", content):
    return PlayerChatMessageLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      player_id=int(pattern_match.group('player_id')),
      message=pattern_match.group('message'),
    )

  if pattern_match := re.match(r"\[CHAT\] (?P<player_name>.+) has restocked (?P<depot_name>.+)", content):
    return PlayerRestockedDepotLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      depot_name=pattern_match.group('depot_name'),
    )

  if pattern_match := re.match(r"\[CHAT\] (?P<company_name>.+) is Created by (?P<player_name>.+)", content):
    return PlayerCreatedCompanyLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      company_name=pattern_match.group('company_name'),
    )

  if pattern_match := re.match(r"\[CHAT\] (?P<message>\S.*)", content):
    return AnnouncementLogEvent(
      timestamp=timestamp,
      message=pattern_match.group('message'),
    )

  if pattern_match := re.match(r"Player Login: (?P<player_name>.+) \((?P<player_id>\d+)\)", content):
    return PlayerLoginLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      player_id=int(pattern_match.group('player_id')),
    )

  if pattern_match := re.match(r"Player Logout: (?P<player_name>.+) \((?P<player_id>\d+)\)", content):
    return PlayerLogoutLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      player_id=int(pattern_match.group('player_id')),
    )

  if pattern_match := re.match(r"Player Logout: (?P<player_name>.+)", content):
    return LegacyPlayerLogoutLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
    )

  if pattern_match := re.match(r"Player level changed. Player=(?P<player_name>.+) \((?P<player_id>\d+)\) Level=(?P<level_type>[^(]+)\((?P<level_value>\d+)\)", content):
    return PlayerLevelChangedLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      player_id=int(pattern_match.group('player_id')),
      level_type=pattern_match.group('level_type'),
      level_value=int(pattern_match.group('level_value')),
    )

  if pattern_match := re.match(r"Player entered vehicle. Player=(?P<player_name>.+) \((?P<player_id>\d+)\) Vehicle=(?P<vehicle_name>[^(]+)\((?P<vehicle_id>\d+)\)", content):
    return PlayerEnteredVehicleLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      player_id=int(pattern_match.group('player_id')),
      vehicle_name=pattern_match.group('vehicle_name'),
      vehicle_id=int(pattern_match.group('vehicle_id')),
    )

  if pattern_match := re.match(r"Player exited vehicle. Player=(?P<player_name>.+) \((?P<player_id>\d+)\) Vehicle=(?P<vehicle_name>[^(]+)\((?P<vehicle_id>\d+)\)", content):
    return PlayerExitedVehicleLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      player_id=int(pattern_match.group('player_id')),
      vehicle_name=pattern_match.group('vehicle_name'),
      vehicle_id=int(pattern_match.group('vehicle_id')),
    )

  if pattern_match := re.match(r"Player bought vehicle. Player=(?P<player_name>.+) \((?P<player_id>\d+)\) Vehicle=(?P<vehicle_name>[^(]+)\((?P<vehicle_id>\d+)\)", content):
    return PlayerBoughtVehicleLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      player_id=int(pattern_match.group('player_id')),
      vehicle_name=pattern_match.group('vehicle_name'),
      vehicle_id=int(pattern_match.group('vehicle_id')),
    )

  if pattern_match := re.match(r"Player sold vehicle. Player=(?P<player_name>.+) \((?P<player_id>\d+)\) Vehicle=(?P<vehicle_name>[^(]+)\((?P<vehicle_id>\d+)\)", content):
    return PlayerSoldVehicleLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      player_id=int(pattern_match.group('player_id')),
      vehicle_name=pattern_match.group('vehicle_name'),
      vehicle_id=int(pattern_match.group('vehicle_id')),
    )

  if pattern_match := re.match(r"Company added. Name=(?P<company_name>[^(]+)\(Corp\?(?P<is_corp>\w+)\) Owner=(?P<owner_name>.+)\((?P<owner_id>\d+)\)", content):
    return CompanyAddedLogEvent(
      timestamp=timestamp,
      company_name=pattern_match.group('company_name'),
      is_corp=pattern_match.group('is_corp') == 'true',
      owner_name=pattern_match.group('owner_name'),
      owner_id=int(pattern_match.group('owner_id')),
    )

  if pattern_match := re.match(r"Company removed. Name=(?P<company_name>[^(]+)\(Corp\?(?P<is_corp>\w+)\) Owner=(?P<owner_name>.+)\((?P<owner_id>\d+)\)", content):
    return CompanyRemovedLogEvent(
      timestamp=timestamp,
      company_name=pattern_match.group('company_name'),
      is_corp=pattern_match.group('is_corp') == 'true',
      owner_name=pattern_match.group('owner_name'),
      owner_id=int(pattern_match.group('owner_id')),
    )

  if pattern_match := re.match(r"[Security Alert]: \[(?P<player_name>.+):(?P<player_id>\d+)\] (?P<message>.+)", content):
    return SecurityAlertLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      player_id=int(pattern_match.group('player_id')),
      message=pattern_match.group('message'),
    )

  if pattern_match := re.match(r"DedicatedServer is started. version: (?P<version>.+)", content):
    return ServerStartedLogEvent(
      timestamp=timestamp,
      version=pattern_match.group('version'),
    )

  return UnknownLogEntry(
    timestamp=timestamp,
    original_line=content
  )


