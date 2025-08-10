from datetime import timedelta
from deepdiff import DeepHash
from django.contrib import admin
from django.contrib.gis.db import models
from django.db.models import Q, F, Sum, Max, Window
from django.db.models.functions import RowNumber, Lead
from decimal import Decimal
from django.core.validators import MinValueValidator, MaxValueValidator
from django.contrib.postgres.fields import ArrayField
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.contrib.auth import get_user_model
from django.contrib.postgres.fields import DateTimeRangeField
from django.contrib.postgres.search import SearchVector
from django.contrib.postgres.indexes import GinIndex
from typing import override, final
from amc.server_logs import (
  PlayerVehicleLogEvent,
  PlayerEnteredVehicleLogEvent,
  PlayerExitedVehicleLogEvent,
  PlayerBoughtVehicleLogEvent,
  PlayerSoldVehicleLogEvent,
)

User = get_user_model()

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
  user = models.OneToOneField(User, models.SET_NULL, related_name='player', null=True)
  adminstrator = models.BooleanField(default=False)

  objects = models.Manager.from_queryset(PlayerQuerySet)()

  @override
  def __str__(self) -> str:
    if self.discord_name:
      return self.discord_name
    character = self.characters.first()
    if character is None:
      return self.unique_id
    return f"{character.name} {self.unique_id}"

  @property
  @admin.display(
    description="Whether user is verified",
    boolean=True,
  )
  def verified(self):
    return self.discord_user_id is not None

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
  async def aget_or_create_character_player(self, player_name, player_id, character_guid=None):
    player, player_created = await Player.objects.aget_or_create(unique_id=player_id)
    character, character_created = await (self.get_queryset()
      .with_last_login()
      .aget_or_create(
        player=player,
        name=player_name,
        defaults={
          'guid': character_guid,
        }
      )
    )
    if character_guid is not None and character.guid is None:
      character.guid = character_guid
      await character.asave(update_fields=['guid'])
    return (character, player, character_created, player_created)


@final
class Character(models.Model):
  player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='characters')
  guid = models.CharField(max_length=32, db_index=True, editable=False, null=True)
  name = models.CharField(max_length=200)
  # levels
  driver_level = models.PositiveIntegerField(null=True)
  bus_level = models.PositiveIntegerField(null=True)
  taxi_level = models.PositiveIntegerField(null=True)
  police_level = models.PositiveIntegerField(null=True)
  truck_level = models.PositiveIntegerField(null=True)
  wrecker_level = models.PositiveIntegerField(null=True)
  racer_level = models.PositiveIntegerField(null=True)
  saving_rate = models.DecimalField(
    max_digits=3,
    decimal_places=2,
    null=True,
    blank=True,
    validators=[
      MinValueValidator(Decimal('0.00')),
      MaxValueValidator(Decimal('1.00'))
    ]
  )

  objects = CharacterManager.from_queryset(CharacterQuerySet)()

  @override
  def __str__(self):
    return f"{self.name} ({self.player.unique_id})"


@final
class Team(models.Model):
  name = models.CharField(max_length=200)
  tag = models.CharField(max_length=6)
  description = models.TextField(blank=True)
  discord_thread_id = models.PositiveBigIntegerField(unique=True)
  owners = models.ManyToManyField(Player, related_name='teams_owned')
  logo = models.FileField(upload_to="team_logos", null=True, blank=True)
  bg_color = models.CharField(max_length=6, default="FFFFFF")
  text_color = models.CharField(max_length=6, default="000000")
  racing = models.BooleanField(default=True)

  players = models.ManyToManyField(
    Player,
    through='TeamMembership',
    related_name='teams'
  )

  @override
  def __str__(self):
    return f"[{self.tag}] {self.name}"


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
  name = models.CharField(max_length=200, null=True)

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
  def route_name(self):
    if self.name is not None:
      return self.name
    return self.config.get('Route', {}).get('RouteName')

  @property
  def num_laps(self):
    return self.config.get('NumLaps', 0)

  @property
  def vehicles(self):
    # TODO map strings
    return self.config.get('VehicleKeys', [])

  @property
  def engines(self):
    # TODO map strings
    return self.config.get('EngineKeys', [])

  @property
  def num_sections(self):
    return len(self.waypoints)

  @property
  def waypoints(self):
    return self.config.get('Route', {}).get('Waypoints', [])


@final
class Championship(models.Model):
  name = models.CharField(max_length=200)
  discord_thread_id = models.CharField(max_length=32, null=True, blank=True, unique=True)
  description = models.TextField(blank=True)

  @override
  def __str__(self):
    return self.name


class ScheduledEventQuerySet(models.QuerySet):
  def filter_active_at(self, timestamp):
    return self.filter(
      start_time__lte=timestamp,
      end_time__gte=timestamp,
    )


@final
class ScheduledEvent(models.Model):
  name = models.CharField(max_length=200)
  start_time = models.DateTimeField()
  end_time = models.DateTimeField(null=True, blank=True)
  discord_event_id = models.CharField(max_length=32, null=True, blank=True, unique=True)
  discord_thread_id = models.CharField(max_length=32, null=True, blank=True, unique=True)
  race_setup = models.ForeignKey(RaceSetup, on_delete=models.SET_NULL, null=True, related_name='scheduled_events')
  championship = models.ForeignKey(Championship, on_delete=models.SET_NULL, null=True, blank=True, related_name='scheduled_events')
  players = models.ManyToManyField(
    Player,
    through='ScheduledEventPlayer',
    related_name='scheduled_events'
  )
  description = models.TextField(blank=True)
  time_trial = models.BooleanField(default=False)
  objects = models.Manager.from_queryset(ScheduledEventQuerySet)()

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
  discord_message_id = models.PositiveBigIntegerField(null=True)
  owner = models.ForeignKey(Character, models.SET_NULL, null=True, blank=True)

  characters = models.ManyToManyField(
    Character,
    through='GameEventCharacter',
    related_name='game_events'
  )

  @override
  def __str__(self):
    return self.name

class ParticipantQuerySet(models.QuerySet):
  def filter_best_time_per_player(self):
    return self.alias(
      p_rank=Window(
        expression=RowNumber(),
        partition_by=[F('character')],
        order_by=[F('disqualified').asc(), F('finished').desc(), F('net_time').asc()]
      )
    ).filter(p_rank=1)

  def filter_by_scheduled_event(self, scheduled_event):
    if scheduled_event.time_trial:
      criteria = Q(
        game_event__race_setup=scheduled_event.race_setup,
        game_event__start_time__gte=scheduled_event.start_time,
        game_event__start_time__lte=scheduled_event.end_time,
      )
    else:
      criteria = Q(game_event__scheduled_event=scheduled_event)
    return self.filter(criteria)

  def results_for_scheduled_event(self, scheduled_event):
    return (self
      .select_related(
        'character',
        'character__player',
        'championship_point',
        'championship_point__team'
      )
      .filter_by_scheduled_event(scheduled_event)
      .filter_best_time_per_player()
      .order_by(
        'disqualified',
        'wrong_engine',
        'wrong_vehicle',
        '-finished',
        'laps',
        'section_index',
        'net_time'
      )
    )

@final
class GameEventCharacter(models.Model):
  character = models.ForeignKey(Character, on_delete=models.CASCADE)
  game_event = models.ForeignKey(GameEvent, on_delete=models.CASCADE, related_name='participants')
  rank = models.IntegerField() # raw game value
  laps = models.IntegerField(default=0)
  section_index = models.IntegerField(default=-1)
  first_section_total_time_seconds = models.FloatField(null=True, blank=True)
  last_section_total_time_seconds = models.FloatField(null=True, blank=True)
  penalty_seconds = models.FloatField(default=0)
  net_time = models.GeneratedField(
    expression=F('last_section_total_time_seconds') - F('first_section_total_time_seconds') + F('penalty_seconds'),
    output_field=models.FloatField(null=True, blank=True),
    db_persist=True,
  )
  best_lap_time = models.FloatField(null=True, blank=True)
  lap_times = ArrayField( # raw game value
    models.FloatField(),
    default=list,
    null=True,
    blank=True,
  )
  wrong_engine = models.BooleanField(default=False)
  wrong_vehicle = models.BooleanField(default=False)
  disqualified = models.BooleanField(default=False)
  finished = models.BooleanField(default=False)
  objects = models.Manager.from_queryset(ParticipantQuerySet)()

  class Meta:
    ordering = ['disqualified', '-finished', '-laps', '-section_index', 'net_time']
    constraints = [
      models.UniqueConstraint(
        fields=["character", "game_event"], name="unique_character_game_event"
      )
    ]


class ChampionshipPointQuerySet(models.QuerySet):
  def personal_standings(request, championship_id):
    return ChampionshipPoint.objects.filter(
      championship=championship_id,
    ).values('participant__character').annotate(
      total_points=Sum('points'),
      player_id=F('participant__character__player__unique_id'),
      character_name=F('participant__character__name'),
    ).order_by('-total_points')

  def team_standings(request, championship_id):
    top_results_subquery = (ChampionshipPoint.objects
      .select_related('team')
      .filter(
        championship=championship_id,
        team__isnull=False,
      )
      .annotate(
        team_pos=Window(
          expression=RowNumber(),
          partition_by=[F('team'), F('participant__game_event__scheduled_event')],
          order_by=[F('points').desc()]
        )
      )
      .filter(team_pos__lte=2)
    )
    return (ChampionshipPoint.objects
      .filter(pk__in=top_results_subquery.values('pk'))
      .values('team__id', 'team__tag', 'team__name')
      .annotate(total_points=Sum('points'))
      .order_by('-total_points')
    )

@final
class ChampionshipPoint(models.Model):
  championship = models.ForeignKey(Championship, models.SET_NULL, null=True)
  participant = models.OneToOneField(GameEventCharacter, models.CASCADE, related_name='championship_point')
  team = models.ForeignKey(Team, models.SET_NULL, null=True, blank=True)
  points = models.PositiveIntegerField(default=0, blank=True)

  objects = models.Manager.from_queryset(ChampionshipPointQuerySet)()

  event_points_by_position = [25, 20, 16, 13, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
  time_trial_points_by_position = [10, 8, 6, 5, 4, 3, 2, 1]

  @classmethod
  def get_event_points_for_position(self, position: int, time_trial: bool=False):
    try:
      if time_trial:
        return self.time_trial_points_by_position[position]
      return self.event_points_by_position[position]
    except IndexError:
      return 0

  @classmethod
  def get_time_trial_points_for_position(self, position: int):
    try:
      return self.time_trial_points_by_position[position]
    except IndexError:
      return 0

class LapSectionTimeQuerySet(models.QuerySet):
  def annotate_net_time(self):
    return self.annotate(
      net_time=F('total_time_seconds') - F('game_event_character__first_section_total_time_seconds')
    )

  def annotate_deltas(self):
    return self.annotate(
      section_duration=Window(
        expression=Lead('total_time_seconds'),
        partition_by=[F('game_event_character')],
        order_by=[F('lap').asc(), F('section_index').asc()]
      ),
    )



@final
class LapSectionTime(models.Model):
  game_event_character = models.ForeignKey(GameEventCharacter, on_delete=models.CASCADE, related_name='lap_section_times')
  section_index = models.IntegerField()
  lap = models.IntegerField()
  rank = models.IntegerField()
  total_time_seconds = models.FloatField()
  objects = models.Manager.from_queryset(LapSectionTimeQuerySet)()

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
  hostname = models.CharField(max_length=100, default='asean-mt-server')
  tag = models.CharField(max_length=100, default='mt-server')
  text = models.TextField()
  event_processed = models.BooleanField(default=False)

  class Meta:
    constraints = [
      models.UniqueConstraint(
        fields=['timestamp', 'text'],
        name='unique_event_log_entry'
      )
    ]
    indexes = [
      GinIndex(
        SearchVector('text', config='english'),
        name='log_text_search_idx',
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

  class Meta:
    indexes = [
      GinIndex(
        SearchVector('text', config='english'),
        name='chat_text_search_idx',
      )
    ]


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

@final
class PlayerMailMessage(models.Model):
  from_player = models.ForeignKey(Player, models.CASCADE, related_name='outbox_messages', null=True, blank=True)
  to_player = models.ForeignKey(Player, models.CASCADE, related_name='inbox_messages')
  content = models.TextField()
  sent_at = models.DateTimeField(editable=False, auto_now_add=True)
  received_at = models.DateTimeField(editable=False, null=True, blank=True)


@final
class DeliveryPoint(models.Model):
  guid = models.CharField(max_length=200, primary_key=True)
  name = models.CharField(max_length=200)
  type = models.CharField(max_length=200)
  coord = models.PointField(srid=0, dim=3)
  data = models.JSONField(null=True, blank=True)
  last_updated = models.DateTimeField(editable=False, auto_now=True)


@final
class CharacterAFKReminder(models.Model):
  character = models.ForeignKey(Character, on_delete=models.CASCADE, related_name='afk_reminders')
  destination = models.PointField(srid=0, dim=3)
  created_at = models.DateTimeField(editable=False, auto_now_add=True)


@final
class ServerCargoArrivedLog(models.Model):
  timestamp = models.DateTimeField()
  player = models.ForeignKey(Player, on_delete=models.SET_NULL, null=True, related_name='delivered_cargos')
  cargo_key = models.CharField(max_length=200, db_index=True)
  payment = models.PositiveBigIntegerField()
  weight = models.FloatField(null=True, blank=True)
  damage = models.FloatField(null=True, blank=True)
  data = models.JSONField(null=True, blank=True)


@final
class ServerSignContractLog(models.Model):
  timestamp = models.DateTimeField()
  player = models.ForeignKey(Player, on_delete=models.SET_NULL, null=True, related_name='contracts_signed')
  cargo_key = models.CharField(max_length=200, db_index=True)
  amount = models.FloatField()
  cost = models.PositiveIntegerField()
  payment = models.PositiveIntegerField()
  delivered = models.BooleanField(default=False)


