from django.db import models
from django.utils import timezone


class Player(models.Model):
  discord_user_id = models.PositiveBigIntegerField(unique=True)
  unique_id = models.PositiveBigIntegerField(unique=True)
  discord_name = models.CharField(max_length=200)
  def __str__(self):
      return self.discord_name


class Team(models.Model):
  name = models.CharField(max_length=200)
  tag = models.CharField(max_length=6)
  discord_thread_id = models.PositiveBigIntegerField(unique=True)

  # This sets up the many-to-many relationship with Player,
  # specifying that the TeamMembership model should be used as the
  # intermediary ("through") table.
  players = models.ManyToManyField(
      Player,
      through='TeamMembership',
      related_name='teams'
  )

  def __str__(self):
      return self.name


class TeamMembership(models.Model):
  """
  This is the "through" model that connects a Player to a Team.
  It allows us to store extra data about the specific relationship
  between a player and a team, such as when they joined.
  """
  player = models.ForeignKey(Player, on_delete=models.CASCADE)
  team = models.ForeignKey(Team, on_delete=models.CASCADE)
  date_joined = models.DateTimeField(default=timezone.now)

  # You could add other fields here to describe the membership, for example:
  # is_captain = models.BooleanField(default=False)
  # role = models.CharField(max_length=100, blank=True)

  class Meta:
    # This constraint ensures that a player can only be a member
    # of a specific team once.
    unique_together = ('player', 'team')

  def __str__(self):
    return f"{self.player.discord_name} in {self.team.name}"

