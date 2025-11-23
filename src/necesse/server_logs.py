import re
from abc import ABC
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
  message: str

@dataclass(frozen=True)
class PlayerLoginLogEvent(BaseLogEvent):
  """Represents a player successfully logging into the server."""
  player_name: str

@dataclass(frozen=True)
class PlayerLogoutLogEvent(BaseLogEvent):
  """Represents a player logging out."""
  player_name: str

@dataclass(frozen=True)
class CommandInvokedLogEvent(BaseLogEvent):
  """Represents a player logging out."""
  command: str
  args: str

@dataclass(frozen=True)
class PrintLogEvent(BaseLogEvent):
  """Represents a player logging out."""
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
  | PlayerLoginLogEvent
  | PlayerLogoutLogEvent
  | CommandInvokedLogEvent
  | PrintLogEvent
  | UnknownLogEntry
)

# [2025-11-21 01:31:47] (freeman): so u cut all  his trees? XD
GAME_TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M:%S'

def parse_log_line(line: str) -> tuple[ServerLog, LogEvent]:
  pattern = r"^(?P<timestamp>\S+)\s+(?P<hostname>\S+)\s+(?P<tag>\S+)\s+(?P<filepath>.*\/)(?P<filename>.*?)\s+\[(?P<game_timestamp>.*?)\]\s+(?P<content>.*)$"
  pattern_match = re.search(pattern, line)

  if not pattern_match:
    return ServerLog(timestamp=timezone.now(), content=line, log_path="", hostname="", tag=""), UnknownLogEntry(timestamp=timezone.now(), original_line=line)

  timestamp = pattern_match.group('timestamp')
  hostname = pattern_match.group('hostname')
  tag = pattern_match.group('tag')
  filename = pattern_match.group('filename')
  game_timestamp = pattern_match.group('game_timestamp')
  content = pattern_match.group('content')
  timestamp = datetime.strptime(game_timestamp.strip('[').strip(']'), GAME_TIMESTAMP_FORMAT).replace(tzinfo=ZoneInfo('Asia/Bangkok'))
  server_log = ServerLog(
    timestamp=timestamp,
    content=content,
    log_path=filename,
    hostname=hostname,
    tag=tag,
  )
  return server_log, parse_log_content(timestamp, content)

def parse_log_content(timestamp, content):
  if pattern_match := re.match(r">\s+(?P<command>\S+)\s+(?P<args>.+)$", content):
    return CommandInvokedLogEvent(
      timestamp=timestamp,
      command=pattern_match.group('command'),
      args=pattern_match.group('args'),
    )
  if pattern_match := re.match(r"\(Print\): (?P<message>.+)$", content):
    return PrintLogEvent(
      timestamp=timestamp,
      message=pattern_match.group('message'),
    )

  if pattern_match := re.match(r"\((?P<player_name>.+)\): (?P<message>.+)$", content):
    return PlayerChatMessageLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
      message=pattern_match.group('message'),
    )

  if pattern_match := re.match(r"Client \"(?P<player_name>.+)\" connected on slot \d+/10\.$", content):
    return PlayerLoginLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
    )

  if pattern_match := re.match(r"Player \d+ \(\"(?P<player_name>.+)\"\) disconnected with message: \w+$", content):
    return PlayerLogoutLogEvent(
      timestamp=timestamp,
      player_name=pattern_match.group('player_name'),
    )

  return UnknownLogEntry(
    timestamp=timestamp,
    original_line=content
  )


