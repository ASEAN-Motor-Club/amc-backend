import asyncstdlib as a
from datetime import timedelta
from deepdiff import DeepHash
from django.contrib import admin
from django.contrib.gis.db import models
from django.db.models import Q, F, Sum, Max, Window, Count
from django.db.models.functions import RowNumber, Lead, Lag
from django.db.models.lookups import GreaterThan, GreaterThanOrEqual
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
from amc.mod_server import spawn_dealership
from amc.enums import CargoKey, VehicleKey

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
  discord_user_id = models.PositiveBigIntegerField(unique=True, null=True, blank=True)
  discord_name = models.CharField(max_length=200, null=True, blank=True)
  user = models.OneToOneField(User, models.SET_NULL, related_name='player', null=True, blank=True)
  adminstrator = models.BooleanField(default=False)
  social_score = models.IntegerField(default=0)
  notes = models.TextField(blank=True)

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

  async def get_latest_character(self):
    character = await (self.characters.with_last_login()
      .filter(last_login__isnull=False)
      .alatest('last_login')
    )
    return character

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
  money = models.PositiveIntegerField(null=True)
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
  reject_ubi = models.BooleanField(default=False)
  ubi_multiplier = models.FloatField(default=1.0)

  objects = CharacterManager.from_queryset(CharacterQuerySet)()

  @override
  def __str__(self):
    return f"{self.name} ({self.player.unique_id})"
  class Meta:
    constraints = [
      models.CheckConstraint(
        condition=Q(saving_rate__gte=0) & Q(saving_rate__lte=1),
        name="saving_rate_between_0_1",
      )
    ]


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
  lateral_spacing = models.IntegerField(default=600, help_text='Horizonal spacing between starting grid')
  longitudinal_spacing = models.IntegerField(default=1000, help_text='Vertical spacing between starting grid')
  initial_offset = models.IntegerField(default=1000, help_text='Gap between starting line and first row')
  pole_side_right = models.BooleanField(default=True, help_text='If true, the first position is on the right side')
  reverse_starting_direction = models.BooleanField(default=False, help_text='If true, the starting grid will be on the opposite side of the starting line')

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

  personal_prize_by_position = [
    4_800_000,
    2_400_000,
    1_440_000,
    960_000,
    720_000,
    480_000,
    360_000,
    300_000,
    300_000,
    240_000,
  ]
  team_prize_by_position = [
    2_700_000,
    1_500_000,
    900_000,
    600_000,
    300_000,
  ]

  @override
  def __str__(self):
    return self.name

  async def calculate_personal_prizes(self):
    personal_standings = ChampionshipPoint.objects.personal_standings(self.id)[:len(self.personal_prize_by_position)]
    return [
      (
        await Character.objects.select_related('player').aget(pk=standing['character_id']),
        self.personal_prize_by_position[i]
      )
      async for i, standing in a.builtins.enumerate(personal_standings)
    ]

  async def calculate_team_prizes(self):
    team_standings = ChampionshipPoint.objects.team_standings(self.id)[:len(self.team_prize_by_position)]

    async def calculate_team_member_prizes(standing, total_team_prize):
      total_participations = await ChampionshipPoint.objects.filter(championship=self, team__id=standing['team__id']).acount()
      member_contributions = ChampionshipPoint.objects.filter(championship=self, team__id=standing['team__id']).values('participant__character').annotate(
        points=Count('id'),
        character_id=F('participant__character__id')
      )
      return [
        (
          await Character.objects.select_related('player').aget(pk=member_contribution['character_id']),
          total_team_prize * member_contribution['points'] / total_participations
        )
        async for member_contribution in member_contributions
      ]

    return [
      character_prize
      async for i, standing in a.builtins.enumerate(team_standings)
      for character_prize in await calculate_team_member_prizes(standing, self.team_prize_by_position[i])
    ]


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
  staggered_start_delay = models.PositiveIntegerField(default=0, help_text="Delay between staggered start, in seconds. This can be overridden in the game")
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
      character_id=F('participant__character__id'),
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
  prize = models.PositiveIntegerField(default=0, blank=True)

  objects = models.Manager.from_queryset(ChampionshipPointQuerySet)()

  event_prize_by_position = [
    450_000,
    270_000,
    180_000,
    150_000,
    120_000,
    90_000,
    75_000,
    60_000,
    52_500,
    52_500,
  ]
  time_trial_prize_by_position = [
    150_000,
    90_000,
    60_000,
    50_000,
    40_000,
    30_000,
    25_000,
    20_000,
    17_500,
    17_500,
  ]
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

  @classmethod
  def get_event_prize_for_position(self, position: int, time_trial: bool=False, base_pay=50_000):
    try:
      if time_trial:
        return self.time_trial_prize_by_position[position] + base_pay
      return self.event_prize_by_position[position] + base_pay
    except IndexError:
      return base_pay


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
  description = models.CharField(max_length=250, blank=True)
  owner = models.ForeignKey(Character, on_delete=models.CASCADE)
  is_corp = models.BooleanField()
  first_seen_at = models.DateTimeField(blank=True)
  money = models.IntegerField(null=True, blank=True)
  

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

class CharacterLocationManager(models.Manager):
  def filter_character_activity(self, character, start_time, end_time):
    return self.filter(character=character, timestamp__gte=start_time, timestamp__lt=end_time).annotate(
      prev_location=Window(
        expression=Lag('location'),
        partition_by=[F('character')],
        order_by=[F('timestamp').asc()]
      )
    )

@final
class CharacterLocation(models.Model):
  timestamp = models.DateTimeField(db_index=True, auto_now_add=True)
  character = models.ForeignKey(Character, on_delete=models.CASCADE, related_name='locations')
  location = models.PointField(srid=0, dim=3)
  vehicle_key = models.CharField(max_length=100, null=True, choices=VehicleKey)
  objects = CharacterLocationManager() 

  class Meta:
    constraints = [
      models.UniqueConstraint(
        fields=['timestamp', 'character'],
        name='unique_character_location'
      )
    ]

  @classmethod
  async def get_character_activity(self, character, start_time, end_time, afk_treshold=1000, teleport_treshold=10000):
    qs = self.objects.filter_character_activity(character, start_time, end_time)
    if not await qs.aexists():
      return (False, False)

    total_dis = 0
    async for cl in qs:
      if cl.prev_location is None:
        continue
      dis = cl.prev_location.distance(cl.location)
      if dis > teleport_treshold:
        continue
      total_dis += dis
      if total_dis > afk_treshold:
        return (True, True)
    return (True, False)


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
  last_updated = models.DateTimeField(editable=False, auto_now=True, null=True)

  def __str__(self):
    return f"{self.name} ({self.type})"

  class Meta:
    ordering = ['name']

class DeliveryPointStorage(models.Model):
  class Kind(models.TextChoices):
    INPUT = "IN", "Input"
    OUTPUT = "OU", "Output"

  delivery_point = models.ForeignKey(DeliveryPoint, models.CASCADE, related_name="storages")
  kind = models.CharField(max_length=2, choices=Kind)
  cargo_key = models.CharField(max_length=200, db_index=True, choices=CargoKey)
  cargo = models.ForeignKey('Cargo', models.CASCADE, related_name='storages', null=True)
  amount = models.PositiveIntegerField()
  capacity = models.PositiveIntegerField(null=True)

@final
class CharacterAFKReminder(models.Model):
  character = models.ForeignKey(Character, on_delete=models.CASCADE, related_name='afk_reminders')
  destination = models.PointField(srid=0, dim=3)
  created_at = models.DateTimeField(editable=False, auto_now_add=True)


@final
class Delivery(models.Model):
  timestamp = models.DateTimeField()
  character = models.ForeignKey(Character, on_delete=models.SET_NULL, null=True, related_name='deliveries')
  cargo_key = models.CharField(max_length=200, db_index=True, choices=CargoKey)
  quantity = models.PositiveIntegerField()
  payment = models.PositiveBigIntegerField()
  subsidy = models.PositiveBigIntegerField(default=0)
  sender_point = models.ForeignKey('DeliveryPoint', models.SET_NULL, null=True, blank=True, related_name='batch_deliveries_out')
  destination_point = models.ForeignKey('DeliveryPoint', models.SET_NULL, null=True, blank=True, related_name='batch_deliveries_in')
  job = models.ForeignKey('DeliveryJob', models.SET_NULL, null=True, blank=True, related_name='deliveries')

@final
class ServerCargoArrivedLog(models.Model):
  timestamp = models.DateTimeField()
  player = models.ForeignKey(Player, on_delete=models.SET_NULL, null=True, related_name='delivered_cargos')
  character = models.ForeignKey(Character, on_delete=models.SET_NULL, null=True, related_name='delivered_cargos')
  cargo_key = models.CharField(max_length=200, db_index=True, choices=CargoKey)
  payment = models.PositiveBigIntegerField()
  weight = models.FloatField(null=True, blank=True)
  damage = models.FloatField(null=True, blank=True)
  sender_point = models.ForeignKey('DeliveryPoint', models.SET_NULL, null=True, blank=True, related_name='deliveries_out')
  destination_point = models.ForeignKey('DeliveryPoint', models.SET_NULL, null=True, blank=True, related_name='deliveries_in')
  data = models.JSONField(null=True, blank=True)


@final
class ServerSignContractLog(models.Model):
  timestamp = models.DateTimeField()
  guid = models.CharField(max_length=32, db_index=True, editable=False, null=True)
  player = models.ForeignKey(Player, on_delete=models.SET_NULL, null=True, related_name='contracts_signed')
  cargo_key = models.CharField(max_length=200, db_index=True)
  amount = models.FloatField()
  finished_amount = models.FloatField(default=0)
  cost = models.PositiveIntegerField()
  payment = models.PositiveIntegerField()
  delivered = models.BooleanField(default=False)
  data = models.JSONField(null=True, blank=True)


@final
class ServerPassengerArrivedLog(models.Model):
  class PassengerType(models.IntegerChoices):
    Unknown = 0,
    Hitchhiker = 1,
    Taxi = 2,
    Ambulance = 3,
    Bus = 4,

  timestamp = models.DateTimeField()
  player = models.ForeignKey(Player, on_delete=models.SET_NULL, null=True, related_name='passengers_delivered')
  passenger_type = models.IntegerField(db_index=True, choices=PassengerType)
  distance = models.FloatField()
  payment = models.PositiveIntegerField()
  arrived = models.BooleanField(default=True)
  comfort = models.BooleanField(null=True)
  urgent = models.BooleanField(null=True)
  limo = models.BooleanField(null=True)
  offroad = models.BooleanField(null=True)
  comfort_rating = models.IntegerField(null=True)
  urgent_rating = models.IntegerField(null=True)
  data = models.JSONField(null=True, blank=True)


@final
class ServerTowRequestArrivedLog(models.Model):
  timestamp = models.DateTimeField()
  player = models.ForeignKey(Player, on_delete=models.SET_NULL, null=True, related_name='tow_requests_delivered')
  payment = models.PositiveIntegerField()
  data = models.JSONField(null=True, blank=True)


@final
class TeleportPoint(models.Model):
  name = models.CharField(max_length=20)
  character = models.ForeignKey(
    Character,
    on_delete=models.SET_NULL,
    related_name='teleport_points',
    null=True,
    blank=True,
  )
  location = models.PointField(srid=0, dim=3)

  class Meta:
    constraints = [
      models.UniqueConstraint(
        fields=['name', 'character'],
        name='unique_character_teleport_point'
      )
    ]

@final
class VehicleDealership(models.Model):
  vehicle_key = models.CharField(max_length=100, null=True, choices=VehicleKey)
  location = models.PointField(srid=0, dim=3)
  yaw = models.FloatField()
  spawn_on_restart = models.BooleanField(default=True)
  notes = models.TextField(blank=True)

  async def spawn(self, http_client_mod):
    await spawn_dealership(
      http_client_mod,
      self.vehicle_key,
      {'X': self.location.x, 'Y': self.location.y, 'Z': self.location.z},
      self.yaw
    )


@final
class Thank(models.Model):
  sender_character = models.ForeignKey(Character, on_delete=models.CASCADE, related_name='thanks_given')
  recipient_character = models.ForeignKey(Character, on_delete=models.CASCADE, related_name='thanks_received')
  timestamp = models.DateTimeField()

@final
class Cargo(models.Model):
  key = models.CharField(max_length=200, primary_key=True)
  label = models.CharField(max_length=200)

  def __str__(self):
    return self.label


class DeliveryJobQuerySet(models.QuerySet):
  def filter_active(self):
    now = timezone.now()
    return self.filter(
      fulfilled=False,
      requested_at__lte=now,
      expired_at__gte=now
    )

  def annotate_active(self):
    now = timezone.now()
    return self.annotate(
      active=~F('fulfilled') &
      GreaterThan(now, F('requested_at')) &
      GreaterThan(F('expired_at'), now)
    )

  def filter_by_delivery(self, delivery_source, delivery_destination, cargo_key):
    return self.filter(
      Q(source_points=delivery_source) | Q(source_points=None),
      Q(destination_points=delivery_destination) | Q(destination_points=None),
      Q(cargo_key=cargo_key) | Q(cargos__key=cargo_key),
    )



@final
class DeliveryJob(models.Model):
  name = models.CharField(max_length=200, null=True, help_text="Give the job a name so it can be identified")
  cargo_key = models.CharField(max_length=200, db_index=True, choices=CargoKey, null=True, blank=True)
  quantity_requested = models.PositiveIntegerField()
  quantity_fulfilled = models.PositiveIntegerField(default=0)
  requested_at = models.DateTimeField(auto_now_add=True)
  expired_at = models.DateTimeField()
  bonus_multiplier = models.FloatField()
  completion_bonus = models.PositiveIntegerField(default=0)
  cargos = models.ManyToManyField('Cargo', related_name='jobs', blank=True, help_text="Use either Cargo Key or this field for multiple cargo types")
  source_points = models.ManyToManyField('DeliveryPoint', related_name='jobs_out', blank=True)
  destination_points = models.ManyToManyField('DeliveryPoint', related_name='jobs_in', blank=True)
  discord_message_id = models.PositiveBigIntegerField(null=True, blank=True, help_text="For bot use only, leave blank")
  description = models.TextField(blank=True, null=True)
  template = models.BooleanField(default=False, help_text="If true this will be used to create future jobs")
  fulfilled = models.GeneratedField(
    expression=GreaterThanOrEqual(F('quantity_fulfilled'), F('quantity_requested')),
    output_field=models.BooleanField(),
    db_persist=True,
  )

  objects = models.Manager.from_queryset(DeliveryJobQuerySet)()

  def __str__(self):
    return f"{self.quantity_requested}x {self.get_cargo_key_display()} ({self.id})"

@final
class Ticket(models.Model):
  class Infringement(models.TextChoices):
    CLUTERRING = "cluterring", "Cluterring"
    GRIEFING = "griefing", "Griefing"
    TROLLING = "trolling", "Trolling"
    OTHER = "other", "Other"

  character = models.ForeignKey(Character, on_delete=models.CASCADE, related_name='tickets')
  infringement = models.CharField(max_length=200, choices=Infringement)
  notes = models.TextField(blank=True)
  created_at = models.DateTimeField(editable=False, auto_now_add=True)

  @classmethod
  def get_social_score_deduction(self, infringement):
    match infringement:
      case self.Infringement.CLUTERRING:
        social_score_deduction = 3
      case self.Infringement.GRIEFING:
        social_score_deduction = 7
      case self.Infringement.TROLLING:
        social_score_deduction = 10
      case self.Infringement.OTHER:
        social_score_deduction = 1
      case _:
        social_score_deduction = 3
    return social_score_deduction

@final
class ServerStatus(models.Model):
  timestamp = models.DateTimeField(auto_now_add=True)
  fps = models.PositiveIntegerField()

