from django.db import models
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

@final
class Player(models.Model):
  unique_id = models.PositiveBigIntegerField(primary_key=True)
  discord_user_id = models.PositiveBigIntegerField(unique=True, null=True)
  discord_name = models.CharField(max_length=200, null=True)

  @override
  def __str__(self) -> str:
    if self.discord_name:
      return self.discord_name
    return f"Unknown player {self.unique_id}"


@final
class Character(models.Model):
  player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='characters')
  name = models.CharField(max_length=200)
  # levels

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
class PlayerStatusLog(models.Model):
  character = models.ForeignKey(Character, on_delete=models.CASCADE)
  timespan = DateTimeRangeField()
  original_log = models.ForeignKey(ServerLog, on_delete=models.CASCADE, null=True)


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


