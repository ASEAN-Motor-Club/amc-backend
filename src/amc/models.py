from datetime import timedelta
from django.db import models
from django.db.models import F, Sum, Max
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

  objects = models.Manager.from_queryset(CharacterQuerySet)()

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
  character = models.ForeignKey(Character, on_delete=models.CASCADE)
  timestamp = models.DateTimeField()
  prompt = models.TextField()


@final
class SongRequestLog(models.Model):
  character = models.ForeignKey(Character, on_delete=models.CASCADE)
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
  character = models.ForeignKey(Character, on_delete=models.CASCADE)
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
  character = models.ForeignKey(Character, on_delete=models.CASCADE)
  vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE)
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


