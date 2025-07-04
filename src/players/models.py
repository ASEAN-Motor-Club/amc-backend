from django.db import models
from django.utils import timezone


class Player(models.Model):
  unique_id = models.PositiveBigIntegerField(primary_key=True)
  discord_user_id = models.PositiveBigIntegerField(unique=True)
  discord_name = models.CharField(max_length=200, null=True)

  def __str__(self):
      return self.discord_name


class Character(models.Model):
  player = models.ForeignKey(Player, on_delete=models.CASCADE)
  name = models.CharField(max_length=200)
  # levels

  def __str__(self):
      return self.name


class Team(models.Model):
  name = models.CharField(max_length=200)
  tag = models.CharField(max_length=6)
  discord_thread_id = models.PositiveBigIntegerField(unique=True)

  players = models.ManyToManyField(
      Player,
      through='TeamMembership',
      related_name='teams'
  )

  def __str__(self):
      return self.name


class TeamMembership(models.Model):
  player = models.ForeignKey(Player, on_delete=models.CASCADE)
  team = models.ForeignKey(Team, on_delete=models.CASCADE)
  date_joined = models.DateTimeField(default=timezone.now)

  class Meta:
    # This constraint ensures that a player can only be a member
    # of a specific team once.
    unique_together = ('player', 'team')

  def __str__(self):
    return f"{self.player.discord_name} in {self.team.name}"


class PlayerStatusLog(models.Model):
  player = models.ForeignKey(Player, on_delete=models.CASCADE)
  timestamp = models.DateTimeField(auto_now=True)


