from datetime import timedelta
from deepdiff import DeepHash
#from django.db import models
from django.contrib.gis.db import models
from django.db.models import F, Sum, Max
from django.contrib.postgres.fields import ArrayField
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.contrib.postgres.fields import DateTimeRangeField
from typing import override, final
from amc.server_logs import (
  PlayerVehicleLogEvent,
  PlayerEnteredVehicleLogEvent,
  PlayerExitedVehicleLogEvent,
  PlayerBoughtVehicleLogEvent,
  PlayerSoldVehicleLogEvent,
)

class PlayerQuerySet(models.QuerySet):
  def with_total_session_time(self):
    return self.annotate(
      total_session_time=Sum(
        'characters__status_logs__duration',
        default=timedelta(0)
      )
    )
  def with_last_login(self):
    return self.annotate(
      last_login=Max(
        'characters__status_logs__timespan__startswith',
        default=None
      )
    )

@final
class Player(models.Model):
  unique_id = models.PositiveBigIntegerField(primary_key=True)
  discord_user_id = models.PositiveBigIntegerField(unique=True, null=True)
  discord_name = models.CharField(max_length=200, null=True)

  objects = models.Manager.from_queryset(PlayerQuerySet)()

  @override
  def __str__(self) -> str:
    if self.discord_name:
      return self.discord_name
    return f"Unknown player {self.unique_id}"


class CharacterQuerySet(models.QuerySet):
  def with_total_session_time(self):
    return self.annotate(
      total_session_time=Sum(
        'status_logs__duration',
        default=timedelta(0)
      )
    )

  def with_last_login(self):
    return self.annotate(
      last_login=Max(
        'status_logs__timespan__startswith',
      )
    )

class CharacterManager(models.Manager):
  async def aget_or_create_character_player(self, player_name, player_id):
    player, player_created = await Player.objects.aget_or_create(unique_id=player_id)
    character, character_created = await (self.get_queryset()
      .with_last_login()
      .aget_or_create(player=player, name=player_name)
    )
    return (character, player, character_created, player_created)


@final
class Character(models.Model):
  player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='characters')
  name = models.CharField(max_length=200)
  # levels
  driver_level = models.PositiveIntegerField(null=True)
  bus_level = models.PositiveIntegerField(null=True)
  taxi_level = models.PositiveIntegerField(null=True)
  police_level = models.PositiveIntegerField(null=True)
  truck_level = models.PositiveIntegerField(null=True)
  wrecker_level = models.PositiveIntegerField(null=True)
  racer_level = models.PositiveIntegerField(null=True)

  objects = CharacterManager.from_queryset(CharacterQuerySet)()

  @override
  def __str__(self):
    return f"{self.name} ({self.player.unique_id})"


@final
class Team(models.Model):
  name = models.CharField(max_length=200)
  tag = models.CharField(max_length=6)
  discord_thread_id = models.PositiveBigIntegerField(unique=True)

  players = models.ManyToManyField(
    Player,
    through='TeamMembership',
    related_name='teams'
  )

  @override
  def __str__(self):
    return self.name


@final
class TeamMembership(models.Model):
  player = models.ForeignKey(Player, on_delete=models.CASCADE)
  team = models.ForeignKey(Team, on_delete=models.CASCADE)
  date_joined = models.DateTimeField(default=timezone.now)

  @final
  class Meta:
    # This constraint ensures that a player can only be a member
    # of a specific team once.
    unique_together = ('player', 'team')

  @override
  def __str__(self):
    return f"{self.player.discord_name} in {self.team.name}"


@final
class RaceSetup(models.Model):
  config = models.JSONField(null=True, blank=True)
  hash = models.CharField(max_length=200, unique=True)

  @staticmethod
  def calculate_hash(race_setup):
    hashes = DeepHash(race_setup)
    return hashes[race_setup]

  @override
  def __str__(self):
    try:
      route_name = self.config['Route']['RouteName']
      num_laps = self.config['NumLaps']
      return f"{route_name} ({num_laps} laps) - {self.hash[:8]}"
    except Exception:
      return "Unknown race setup"

  @property
  def num_laps(self):
    return self.config.get('NumLaps', 0)

  @property
  def num_sections(self):
    return len(self.config.get('Route', {}).get('Waypoints', []))

@final
class ScheduledEvent(models.Model):
  name = models.CharField(max_length=200)
  start_time = models.DateTimeField()
  end_time = models.DateTimeField(null=True, blank=True)
  discord_event_id = models.CharField(max_length=32, null=True, blank=True, unique=True)
  discord_thread_id = models.CharField(max_length=32, null=True, blank=True, unique=True)
  race_setup = models.ForeignKey(RaceSetup, on_delete=models.SET_NULL, null=True, related_name='scheduled_events')

  players = models.ManyToManyField(
    Player,
    through='ScheduledEventPlayer',
    related_name='scheduled_events'
  )

  @override
  def __str__(self):
    return self.name


@final
class ScheduledEventPlayer(models.Model):
  player = models.ForeignKey(Player, on_delete=models.CASCADE)
  scheduled_event = models.ForeignKey(ScheduledEvent, on_delete=models.CASCADE)

  class Meta:
    constraints = [
      models.UniqueConstraint(
        fields=["player", "scheduled_event"], name="unique_player_scheduled_event"
      )
    ]


@final
class GameEvent(models.Model):
  name = models.CharField(max_length=200)
  guid = models.CharField(max_length=32, db_index=True, editable=False)
  start_time = models.DateTimeField(editable=False, auto_now_add=True)
  last_updated = models.DateTimeField(editable=False, auto_now=True)
  scheduled_event = models.ForeignKey(ScheduledEvent, on_delete=models.SET_NULL, null=True, blank=True, related_name='game_events')
  race_setup = models.ForeignKey(RaceSetup, on_delete=models.SET_NULL, null=True, related_name='game_events')
  state = models.IntegerField()

  characters = models.ManyToManyField(
    Character,
    through='GameEventCharacter',
    related_name='game_events'
  )

  @override
  def __str__(self):
    return self.name


@final
class GameEventCharacter(models.Model):
  character = models.ForeignKey(Character, on_delete=models.CASCADE)
  game_event = models.ForeignKey(GameEvent, on_delete=models.CASCADE)
  rank = models.IntegerField() # raw game value
  laps = models.IntegerField(default=0)
  section_index = models.IntegerField(default=-1)
  last_section_total_time_seconds = models.FloatField()
  best_lap_time = models.FloatField()
  lap_times = ArrayField( # raw game value
    models.FloatField()
  )
  wrong_engine = models.BooleanField()
  wrong_vehicle = models.BooleanField()
  disqualified = models.BooleanField()
  finished = models.BooleanField()

  class Meta:
    constraints = [
      models.UniqueConstraint(
        fields=["character", "game_event"], name="unique_character_game_event"
      )
    ]


@final
class LapSectionTime(models.Model):
  game_event_character = models.ForeignKey(GameEventCharacter, on_delete=models.CASCADE, related_name='lap_section_times')
  section_index = models.IntegerField()
  lap = models.IntegerField()
  rank = models.IntegerField()
  total_time_seconds = models.FloatField()

  class Meta:
    constraints = [
      models.UniqueConstraint(
        fields=["game_event_character", "section_index", "lap"], name="unique_event_lap_section_time"
      )
    ]



@final
class Vehicle(models.Model):
  id = models.PositiveBigIntegerField(primary_key=True)
  name = models.CharField(max_length=200)

  @override
  def __str__(self):
    return f"{self.name} ({self.id})"


@final
class Company(models.Model):
  name = models.CharField(max_length=200)
  owner = models.ForeignKey(Character, on_delete=models.CASCADE)
  is_corp = models.BooleanField()
  first_seen_at = models.DateTimeField()

  class Meta:
    verbose_name = _('Company')
    verbose_name_plural = _('Companies')

  @override
  def __str__(self):
    return f"{self.name} ({self.id})"


@final
class ServerLog(models.Model):
  timestamp = models.DateTimeField()
  log_path = models.CharField(max_length=500, null=True)
  text = models.TextField()
  event_processed = models.BooleanField(default=False)

  class Meta:
    constraints = [
      models.UniqueConstraint(
        fields=['timestamp', 'text'],
        name='unique_event_log_entry'
      )
    ]


@final
class BotInvocationLog(models.Model):
  character = models.ForeignKey(Character, on_delete=models.CASCADE, related_name='bot_invocation_logs')
  timestamp = models.DateTimeField()
  prompt = models.TextField()


@final
class SongRequestLog(models.Model):
  character = models.ForeignKey(Character, on_delete=models.CASCADE, related_name='song_request_logs')
  timestamp = models.DateTimeField()
  song = models.TextField()


@final
class PlayerStatusLog(models.Model):
  character = models.ForeignKey(Character, on_delete=models.CASCADE, related_name='status_logs')
  timespan = DateTimeRangeField()
  duration = models.GeneratedField(
    expression=F('timespan__endswith') - F('timespan__startswith'),
    output_field=models.DurationField(),
    db_persist=True,
  )
  original_log = models.ForeignKey(ServerLog, on_delete=models.CASCADE, null=True)

  @property
  def login_time(self):
    return self.timespan.lower

  @property
  def logout_time(self):
    return self.timespan.upper

  class Meta:
    ordering = ['-timespan__startswith']


@final
class PlayerChatLog(models.Model):
  character = models.ForeignKey(Character, on_delete=models.CASCADE, related_name='chat_logs')
  timestamp = models.DateTimeField()
  text = models.TextField()


@final
class PlayerRestockDepotLog(models.Model):
  timestamp = models.DateTimeField()
  character = models.ForeignKey(
    Character,
    on_delete=models.CASCADE,
    related_name='restock_depot_logs'
  )
  depot_name = models.CharField(max_length=200)


@final
class PlayerVehicleLog(models.Model):
  class Action(models.TextChoices):
    ENTERED = "EN", _("Entered")
    EXITED = "EX", _("Exited")
    BOUGHT = "BO", _("Bought")
    SOLD = "SO", _("Sold")

  timestamp = models.DateTimeField()
  character = models.ForeignKey(Character, on_delete=models.CASCADE, related_name='vehicle_logs')
  vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, null=True)
  vehicle_game_id = models.PositiveBigIntegerField(null=True, db_index=True)
  vehicle_name = models.CharField(max_length=100, null=True)
  action = models.CharField(max_length=2, choices=Action)

  @classmethod
  def action_for_event(cls, event: PlayerVehicleLogEvent):
    match event:
      case PlayerEnteredVehicleLogEvent():
        return cls.Action.ENTERED
      case PlayerExitedVehicleLogEvent():
        return cls.Action.EXITED
      case PlayerBoughtVehicleLogEvent():
        return cls.Action.BOUGHT
      case PlayerSoldVehicleLogEvent():
        return cls.Action.SOLD
      case _:
        raise ValueError('Unknown vehicle log event')

  class Meta:
    constraints = [
      models.UniqueConstraint(
        fields=['timestamp', 'character', 'vehicle', 'action'],
        name='unique_vehicle_log_entry'
      )
    ]

@final
class CharacterLocation(models.Model):
  timestamp = models.DateTimeField(db_index=True, auto_now_add=True)
  character = models.ForeignKey(Character, on_delete=models.CASCADE, related_name='locations')
  location = models.PointField(srid=0, dim=3)

  class Meta:
    constraints = [
      models.UniqueConstraint(
        fields=['timestamp', 'character'],
        name='unique_character_location'
      )
    ]
