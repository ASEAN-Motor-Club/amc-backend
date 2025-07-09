import re
from abc import ABC
from dataclasses import dataclass
from datetime import datetime
from django.utils import timezone


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
class PlayerCreatedCompanyLogEventLogEvent(BaseLogEvent):
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
class PlayerEnteredVehicleLogEvent(BaseLogEvent):
  """Represents a player logging out."""
  player_name: str
  player_id: int
  vehicle_name: str
  vehicle_id: int

@dataclass(frozen=True)
class PlayerExitedVehicleLogEvent(BaseLogEvent):
  """Represents a player logging out."""
  player_name: str
  player_id: int
  vehicle_name: str
  vehicle_id: int

@dataclass(frozen=True)
class PlayerBoughtVehicleLogEvent(BaseLogEvent):
  """Represents a player logging out."""
  player_name: str
  player_id: int
  vehicle_name: str
  vehicle_id: int

@dataclass(frozen=True)
class PlayerSoldVehicleLogEvent(BaseLogEvent):
  """Represents a player logging out."""
  player_name: str
  player_id: int
  vehicle_name: str
  vehicle_id: int

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
class SecurityAlertLogEvent(BaseLogEvent):
  """Represents a player logging out."""
  player_name: str
  player_id: int
  message: str

@dataclass(frozen=True)
class UnknownLogEntry(BaseLogEvent):
  """Represents a log line that could not be parsed into a known event."""
  original_line: str

LogEvent = (
  PlayerChatMessageLogEvent
  | PlayerCreatedCompanyLogEventLogEvent
  | PlayerLevelChangedLogEvent
  | PlayerLoginLogEvent
  | PlayerLogoutLogEvent
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

def parse_log_line(line: str) -> LogEvent:
  try:
    log_timestamp, _hostname, _tag, _game_timestamp, content = line.split(' ', 4)
    timestamp = datetime.fromisoformat(log_timestamp)
  except ValueError:
    return UnknownLogEntry(timestamp=timezone.now(), original_line=line)

  if pattern_match := re.match(r"\[CHAT\] (?P<player_name>\w+) \((?P<player_id>\d+)\): (?P<message>.+)", content):
    return PlayerChatMessageLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      player_id=int(pattern_match.group('player_id')),
      message=pattern_match.group('message'),
    )

  if pattern_match := re.match(r"\[CHAT\] (?P<player_name>\w+) has restocked (?P<depot_name>.+)", content):
    return PlayerRestockedDepotLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      depot_name=pattern_match.group('depot_name'),
    )

  if pattern_match := re.match(r"\[CHAT\] (?P<company_name>.+) is Created by (?P<player_name>\w+)", content):
    return PlayerCreatedCompanyLogEventLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      company_name=pattern_match.group('company_name'),
    )

  if pattern_match := re.match(r"\[CHAT\] (?P<message>.+)", content):
    return AnnouncementLogEvent(
      timestamp=timestamp,
      message=pattern_match.group('message'),
    )

  if pattern_match := re.match(r"Player Login: (?P<player_name>\w+) \((?P<player_id>\d+)\)", content):
    return PlayerLoginLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      player_id=int(pattern_match.group('player_id')),
    )

  if pattern_match := re.match(r"Player Logout: (?P<player_name>\w+) \((?P<player_id>\d+)\)", content):
    return PlayerLogoutLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      player_id=int(pattern_match.group('player_id')),
    )

  if pattern_match := re.match(r"Player level changed. Player=(?P<player_name>\w+) \((?P<player_id>\d+)\) Level=(?P<level_type>[^(]+)\((?P<level_value>\d+)\)", content):
    return PlayerLevelChangedLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      player_id=int(pattern_match.group('player_id')),
      level_type=pattern_match.group('level_type'),
      level_value=int(pattern_match.group('level_value')),
    )

  if pattern_match := re.match(r"Player entered vehicle. Player=(?P<player_name>\w+) \((?P<player_id>\d+)\) Vehicle=(?P<vehicle_name>[^(]+)\((?P<vehicle_id>\d+)\)", content):
    return PlayerEnteredVehicleLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      player_id=int(pattern_match.group('player_id')),
      vehicle_name=pattern_match.group('vehicle_name'),
      vehicle_id=int(pattern_match.group('vehicle_id')),
    )

  if pattern_match := re.match(r"Player exited vehicle. Player=(?P<player_name>\w+) \((?P<player_id>\d+)\) Vehicle=(?P<vehicle_name>[^(]+)\((?P<vehicle_id>\d+)\)", content):
    return PlayerExitedVehicleLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      player_id=int(pattern_match.group('player_id')),
      vehicle_name=pattern_match.group('vehicle_name'),
      vehicle_id=int(pattern_match.group('vehicle_id')),
    )

  if pattern_match := re.match(r"Player bought vehicle. Player=(?P<player_name>\w+) \((?P<player_id>\d+)\) Vehicle=(?P<vehicle_name>[^(]+)\((?P<vehicle_id>\d+)\)", content):
    return PlayerBoughtVehicleLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      player_id=int(pattern_match.group('player_id')),
      vehicle_name=pattern_match.group('vehicle_name'),
      vehicle_id=int(pattern_match.group('vehicle_id')),
    )

  if pattern_match := re.match(r"Player sold vehicle. Player=(?P<player_name>\w+) \((?P<player_id>\d+)\) Vehicle=(?P<vehicle_name>[^(]+)\((?P<vehicle_id>\d+)\)", content):
    return PlayerSoldVehicleLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      player_id=int(pattern_match.group('player_id')),
      vehicle_name=pattern_match.group('vehicle_name'),
      vehicle_id=int(pattern_match.group('vehicle_id')),
    )

  if pattern_match := re.match(r"Company added. Name=(?P<company_name>[^(]+)\(Corp\?(?P<is_corp>\w+)\) Owner=(?P<owner_name>\w+)\((?P<owner_id>\d+)\)", content):
    return CompanyAddedLogEvent(
      timestamp=timestamp,
      company_name=pattern_match.group('company_name'),
      is_corp=pattern_match.group('is_corp') == 'true',
      owner_name=pattern_match.group('owner_name'),
      owner_id=int(pattern_match.group('owner_id')),
    )

  if pattern_match := re.match(r"Company removed. Name=(?P<company_name>[^(]+)\(Corp\?(?P<is_corp>\w+)\) Owner=(?P<owner_name>\w+)\((?P<owner_id>\d+)\)", content):
    return CompanyRemovedLogEvent(
      timestamp=timestamp,
      company_name=pattern_match.group('company_name'),
      is_corp=pattern_match.group('is_corp') == 'true',
      owner_name=pattern_match.group('owner_name'),
      owner_id=int(pattern_match.group('owner_id')),
    )

  if pattern_match := re.match(r"[Security Alert]: \[(?P<player_name>\w+):(?P<player_id>\d+)\] (?P<message>.+)", content):
    return SecurityAlertLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      player_id=int(pattern_match.group('player_id')),
      message=pattern_match.group('message'),
    )

  return UnknownLogEntry(
    timestamp=timestamp,
    original_line=content
  )


