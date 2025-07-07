from django.db import models
from django.db.models import F, Func
from django.utils import timezone
from typing import override, final

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
  player = models.ForeignKey(Player, on_delete=models.CASCADE)
  name = models.CharField(max_length=200)
  # levels

  @override
  def __str__(self):
    return self.name


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
class PlayerChatLog(models.Model):
  character = models.ForeignKey(Character, on_delete=models.CASCADE)
  timestamp = models.DateTimeField(auto_now=True)
  text = models.TextField()


@final
class ServerLog(models.Model):
  timestamp = models.DateTimeField()
  text = models.TextField()

  class Meta:
    constraints = [
      models.UniqueConstraint(
        fields=['timestamp', 'text'],
        name='unique_event_log_entry'
      )
    ]

