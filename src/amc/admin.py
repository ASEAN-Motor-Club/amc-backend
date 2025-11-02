import aiohttp
import json
from datetime import timedelta
from asgiref.sync import async_to_sync
from django.contrib import admin
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.conf import settings
from django.db.models import F, Count, Window
from django.db.models.functions import RowNumber
from django.contrib import messages
from django.utils.translation import ngettext
from django.contrib.postgres.aggregates import ArrayAgg
from .models import (
  Player,
  Ticket,
  Character,
  Company,
  PlayerChatLog,
  PlayerRestockDepotLog,
  PlayerVehicleLog,
  PlayerStatusLog,
  ServerLog,
  BotInvocationLog,
  SongRequestLog,
  GameEvent,
  GameEventCharacter,
  LapSectionTime,
  CharacterLocation,
  PlayerMailMessage,
  ScheduledEvent,
  RaceSetup,
  Championship,
  ChampionshipPoint,
  Team,
  Delivery,
  DeliveryPoint,
  DeliveryPointStorage,
  ServerCargoArrivedLog,
  ServerSignContractLog,
  ServerPassengerArrivedLog,
  ServerTowRequestArrivedLog,
  TeleportPoint,
  VehicleDealership,
  DeliveryJob,
  Cargo,
  ServerStatus,
  PlayerShift,
  RescueRequest,
)
from amc_finance.services import send_fund_to_player
from amc_finance.admin import AccountInlineAdmin


class CharacterInlineAdmin(admin.TabularInline):
  model = Character
  readonly_fields = ['name']
  show_change_link = True
  fields = ['name', 'last_login', 'total_session_time']
  readonly_fields = ['name', 'last_login', 'total_session_time']

  def last_login(self, character):
    if character.last_login is not None:
      return timezone.localtime(character.last_login)

  def total_session_time(self, character):
    return character.total_session_time

  def get_queryset(self, request):
    qs = super().get_queryset(request)
    return qs.with_last_login().with_total_session_time()


class TicketInlineAdmin(admin.TabularInline):
  model = Ticket
  exclude = ['character']
  autocomplete_fields = ['player', 'issued_by']
  fk_name = 'player'

@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
  list_display = ['id', 'player', 'infringement', 'created_at', 'issued_by']
  search_fields = ['player']
  list_select_related = ['player', 'issued_by']
  list_filter = ['infringement']

class TeamPlayerInlineAdmin(admin.TabularInline):
  model = Team.players.through
  autocomplete_fields = ['player', 'character']
  show_change_link = True

class PlayerTeamInlineAdmin(admin.TabularInline):
  model = Player.teams.through
  show_change_link = True
  autocomplete_fields = ['character']

@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
  list_display = ['unique_id', 'character_names', 'characters_count', 'discord_user_id', 'verified']
  search_fields = ['unique_id', 'characters__name', 'discord_user_id']
  autocomplete_fields = ['user']
  inlines = [CharacterInlineAdmin, PlayerTeamInlineAdmin, TicketInlineAdmin]

  def get_queryset(self, request):
    qs = super().get_queryset(request)
    return qs.annotate(
      character_names=ArrayAgg('characters__name'),
      characters_count=Count('characters'),
    )

  def character_names(self, player):
    return ', '.join(player.character_names)

  @admin.display(ordering="characters_count")
  def characters_count(self, player):
    return player.characters_count


class PlayerStatusLogInlineAdmin(admin.TabularInline):
  model = PlayerStatusLog
  readonly_fields = ['character', 'login_time', 'logout_time', 'duration']
  exclude = ['original_log', 'timespan']

  def login_time(self, log):
    if log.login_time is not None:
      return timezone.localtime(log.login_time)

  def logout_time(self, log):
    if log.logout_time is not None:
      return timezone.localtime(log.logout_time)


@admin.register(Character)
class CharacterAdmin(admin.ModelAdmin):
  list_display = ['name', 'player__unique_id', 'last_login', 'total_session_time']
  list_select_related = ['player']
  search_fields = ['player__unique_id', 'player__discord_user_id', 'name', 'guid']
  inlines = [AccountInlineAdmin, PlayerStatusLogInlineAdmin]
  readonly_fields = ['guid', 'player', 'last_login', 'total_session_time']

  @admin.display(ordering="last_login", boolean=False)
  def last_login(self, obj):
    return obj.last_login

  @admin.display(ordering="total_session_time", boolean=False)
  def total_session_time(self, obj):
    return obj.total_session_time

  def get_queryset(self, request):
    qs = super().get_queryset(request)
    return qs.with_last_login().with_total_session_time().order_by(F('last_login').desc(nulls_last=True))

class PlayerVehicleLogInlineAdmin(admin.TabularInline):
  model = PlayerVehicleLog
  readonly_fields = ['character']
  exclude = ['vehicle']

@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
  list_display = ['name', 'owner', 'is_corp', 'first_seen_at']
  list_filter =  ['is_corp']
  readonly_fields = ['owner']
  search_fields = ['owner__name', 'owner__player__unique_id', 'name']

@admin.register(PlayerChatLog)
class PlayerChatLogAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'character', 'text']
  list_select_related = ['character', 'character__player']
  ordering = ['-timestamp']
  search_fields = ['character__name', 'character__player__unique_id']
  readonly_fields = ['character']

@admin.register(PlayerRestockDepotLog)
class PlayerRestockDepotLogAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'character', 'depot_name']
  list_select_related = ['character', 'character__player']
  ordering = ['-timestamp']
  search_fields = ['character__name', 'character__player__unique_id', 'depot_name']
  readonly_fields = ['character']

@admin.register(BotInvocationLog)
class BotInvocationLogAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'character', 'prompt']
  list_select_related = ['character', 'character__player']
  ordering = ['-timestamp']
  search_fields = ['character__name', 'character__player__unique_id']
  readonly_fields = ['character']

@admin.register(SongRequestLog)
class SongRequestLogAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'character', 'song']
  list_select_related = ['character', 'character__player']
  ordering = ['-timestamp']
  search_fields = ['character__name', 'character__player__unique_id']
  readonly_fields = ['character']

@admin.register(PlayerStatusLog)
class PlayerStatusLogAdmin(admin.ModelAdmin):
  list_display = ['character', 'login_time', 'logout_time', 'duration']
  list_select_related = ['character', 'character__player']
  ordering = [F('timespan__startswith').desc(nulls_last=True)]
  exclude = ['original_log']
  readonly_fields = ['character', 'login_time', 'logout_time']
  search_fields = ['character__name', 'character__player__unique_id']

  def login_time(self, log):
    if log.login_time is not None:
      return timezone.localtime(log.login_time)

  def logout_time(self, log):
    if log.logout_time is not None:
      return timezone.localtime(log.logout_time)

@admin.register(PlayerVehicleLog)
class PlayerVehicleLogAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'character', 'vehicle_game_id', 'vehicle_name', 'action']
  list_select_related = ['character', 'character__player']
  ordering = ['-timestamp']
  search_fields = ['character__name', 'character__player__unique_id', 'vehicle_game_id']
  readonly_fields = ['character']
  exclude = ['vehicle']

@admin.register(ServerLog)
class ServerLogAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'hostname', 'text', 'event_processed']
  ordering = ['-timestamp']
  list_filter = ['event_processed', 'hostname']
  search_fields = ['text']


class GameEventCharacterInlineAdmin(admin.TabularInline):
  model = GameEventCharacter
  readonly_fields = ['character']
  show_change_link = True

class LapSectionTimeInlineAdmin(admin.TabularInline):
  model = LapSectionTime

@admin.register(GameEvent)
class GameEventAdmin(admin.ModelAdmin):
  list_display = ['guid', 'name', 'start_time', 'scheduled_event', 'owner']
  inlines = [GameEventCharacterInlineAdmin]

@admin.register(GameEventCharacter)
class GameEventCharacterAdmin(admin.ModelAdmin):
  list_display = ['id', 'rank', 'character', 'finished', 'net_time', 'game_event', 'game_event__scheduled_event', 'game_event__last_updated']
  list_select_related = ['character', 'character__player', 'game_event', 'game_event__scheduled_event']
  inlines = [LapSectionTimeInlineAdmin]
  readonly_fields = ['character']
  search_fields = ['game_event__id', 'game_event__scheduled_event__name', 'character__name', 'game_event__race_setup__hash']
  list_filter = ['finished', 'game_event__scheduled_event']
  ordering = ['-game_event__last_updated', 'net_time']


class GameEventInlineAdmin(admin.TabularInline):
  model = GameEvent
  fields = ['guid', 'name', 'state', 'start_time']
  readonly_fields = ['guid', 'start_time']
  show_change_link = True

class ScheduledEventInlineAdmin(admin.TabularInline):
  model = ScheduledEvent
  fields = ['name', 'race_setup', 'start_time']
  readonly_fields = ['name', 'race_setup', 'start_time']
  show_change_link = True

@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
  list_display = ['tag', 'name', 'racing']
  search_fields = ['tag', 'name']
  inlines = [TeamPlayerInlineAdmin]
  autocomplete_fields = ['owners']

@admin.register(Championship)
class ChampionshipAdmin(admin.ModelAdmin):
  list_display = ['name']
  inlines = [ScheduledEventInlineAdmin]
  search_fields = ['name']
  actions = ['award_prizes']

  @admin.action(description="Award prizes")
  def award_prizes(self, request, queryset):
    for championship in queryset:
      personal_prizes = async_to_sync(championship.calculate_personal_prizes)()
      team_prizes = async_to_sync(championship.calculate_team_prizes)()

      for character, prize in personal_prizes:
        async_to_sync(send_fund_to_player)(
          prize,
          character,
          f"Championhip Personal Prize: {championship.name}"
        )

      for character, prize in team_prizes:
        async_to_sync(send_fund_to_player)(
          prize,
          character,
          f"Championhip Team Prize: {championship.name}"
        )

@admin.register(ChampionshipPoint)
class ChampionshipPointAdmin(admin.ModelAdmin):
  list_display = ['championship', 'participant__character', 'participant__game_event__scheduled_event__name', 'team', 'points', 'prize']
  list_select_related = ['championship', 'participant__character', 'team', 'participant__game_event__scheduled_event']
  search_fields = ['championship__name', 'participant__character__name', 'team__name']

@admin.register(ScheduledEvent)
class ScheduledEventAdmin(admin.ModelAdmin):
  list_display = ['name', 'race_setup', 'start_time', 'discord_event_id', 'championship', 'time_trial']
  list_select_related = ['race_setup']
  inlines = [GameEventInlineAdmin]
  search_fields = ['name', 'race_setup__hash', 'race_setup__name']
  autocomplete_fields = ['race_setup']
  actions = ['award_points', 'assign_to_game_events']

  @admin.action(description="Assign to game events")
  def assign_to_game_events(self, request, queryset):
    for scheduled_event in queryset:
      assert scheduled_event.time_trial, "Time trials only"
      GameEvent.objects.filter(
        race_setup=scheduled_event.race_setup,
        start_time__gte=scheduled_event.start_time,
        start_time__lte=scheduled_event.end_time,
      ).update(
        scheduled_event=scheduled_event
      )


  @admin.action(description="Award points")
  def award_points(self, request, queryset):
    for scheduled_event in queryset:
      championship = scheduled_event.championship
      # TODO: Create custom ParticipantQuerySet
      participants = (GameEventCharacter.objects
        .select_related('character')
        .filter_by_scheduled_event(scheduled_event)
        .filter(finished=True).filter(
          finished=True,
          disqualified=False,
          wrong_engine=False,
          wrong_vehicle=False,
        )
        .annotate(
          p_rank=Window(
            expression=RowNumber(),
            partition_by=[F('character')],
            order_by=[F('net_time').asc()]
          ),
        )
        .filter(p_rank=1)
        .order_by('net_time')
      )

      def get_participant_team(participant):
        team_membership = participant.character.team_memberships.last()
        if team_membership is None:
          return
        return team_membership.team

      cps = [
        ChampionshipPoint(
          championship=championship,
          participant=participant,
          team=get_participant_team(participant),
          points=ChampionshipPoint.get_event_points_for_position(i, time_trial=scheduled_event.time_trial),
          prize=ChampionshipPoint.get_event_prize_for_position(i, time_trial=scheduled_event.time_trial),
        )
        for i, participant in enumerate(participants)
      ]
      ChampionshipPoint.objects.bulk_create(cps)
      for i, participant in enumerate(participants):
        async_to_sync(send_fund_to_player)(
          ChampionshipPoint.get_event_prize_for_position(i, time_trial=scheduled_event.time_trial),
          participant.character,
          f"Prize money: {scheduled_event.name}"
        )


@admin.register(RaceSetup)
class RaceSetupAdmin(admin.ModelAdmin):
  list_display = ['hash', 'route_name', 'num_laps', 'vehicles', 'engines']
  search_fields = ['hash', 'name']
  inlines = [GameEventInlineAdmin]

@admin.register(CharacterLocation)
class CharacterLocationAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'character', 'location', 'map_link']
  list_select_related = ['character', 'character__player']
  readonly_fields = ['character', 'map_link']
  search_fields = ['character__name', 'character__player__unique_id']

  @admin.display()
  def map_link(self, character_location):
    location = {
      'x': character_location.location.x,
      'y': character_location.location.y,
      'label': character_location.character.name
    }
    pins_str = json.dumps([location])
    return mark_safe(f"<a href='https://www.aseanmotorclub.com/map?pins={pins_str}&focus_index=0' target='_blank'>Open on Map</a>")

@admin.register(PlayerMailMessage)
class PlayerMailMessageAdmin(admin.ModelAdmin):
  list_select_related = ['to_player', 'from_player']
  list_display = ['sent_at', 'to_player', 'received_at', 'content']
  autocomplete_fields = ['to_player', 'from_player']

class DeliveryPointStorageInlineAdmin(admin.TabularInline):
  model = DeliveryPointStorage
  show_change_link = False
  readonly_fields = ['cargo_key']

@admin.register(DeliveryPoint)
class DeliveryPointAdmin(admin.ModelAdmin):
  list_display = ['guid', 'name', 'type', 'coord', 'last_updated']
  search_fields = ['name', 'guid']
  inlines = [DeliveryPointStorageInlineAdmin]

@admin.register(ServerCargoArrivedLog)
class ServerCargoArrivedLogAdmin(admin.ModelAdmin):
  list_display = ['id', 'timestamp', 'player', 'cargo_key', 'payment']
  list_select_related = ['player']
  search_fields = ['player__unique_id', 'cargo_key']
  autocomplete_fields = ['player', 'character', 'sender_point', 'destination_point']

@admin.register(ServerSignContractLog)
class ServerSignContractLogAdmin(admin.ModelAdmin):
  list_display = ['id', 'timestamp', 'player', 'cargo_key', 'amount', 'cost', 'payment', 'delivered']
  list_select_related = ['player']
  search_fields = ['player__unique_id', 'cargo_key']
  autocomplete_fields = ['player']

@admin.register(ServerPassengerArrivedLog)
class ServerPassengerArrivedLogAdmin(admin.ModelAdmin):
  list_display = ['id', 'timestamp', 'player', 'passenger_type', 'payment']
  list_select_related = ['player']
  search_fields = ['player__unique_id']
  autocomplete_fields = ['player']
  list_filter = ['passenger_type']

@admin.register(ServerTowRequestArrivedLog)
class ServerTowRequestArrivedLogAdmin(admin.ModelAdmin):
  list_display = ['id', 'timestamp', 'player', 'payment']
  list_select_related = ['player']
  search_fields = ['player__unique_id']
  autocomplete_fields = ['player']

@admin.register(TeleportPoint)
class TeleportPointAdmin(admin.ModelAdmin):
  list_display = ['id', 'character', 'name', 'location']
  list_select_related = ['character']
  search_fields = ['character__name', 'name', 'character__player__unique_id']
  autocomplete_fields = ['character']

@admin.register(VehicleDealership)
class VehicleDealershipAdmin(admin.ModelAdmin):
  list_display = ['id', 'vehicle_key', 'location', 'spawn_on_restart', 'notes']
  search_fields = ['vehicle_key', 'notes']

  actions = ['spawn']

  @admin.action(description="Spawn Dealerships")
  def spawn(self, request, queryset):
    async def spawn_dealerships():
      http_client_mod = aiohttp.ClientSession(base_url=settings.MOD_SERVER_API_URL)
      async for vd in queryset:
        await vd.spawn(http_client_mod)
    async_to_sync(spawn_dealerships)()

class CargoInlineAdmin(admin.TabularInline):
  model = Cargo
  readonly_fields = ['key', 'label']

@admin.register(Cargo)
class CargoAdmin(admin.ModelAdmin):
  list_display = ['key', 'label', 'type']
  search_fields = ['label']
  list_select_related = ['type']
  inlines = [CargoInlineAdmin]

@admin.register(DeliveryJob)
class DeliveryJobAdmin(admin.ModelAdmin):
  list_display = ['id', 'name', 'completion_bonus', 'finished', 'requested_at', 'template', 'postable', 'num_posted']
  ordering = ['-requested_at']
  search_fields = [
    'name',
    'cargo_key',
    'cargos__label',
    'source_points__name',
    'destination_points__name',
  ]
  autocomplete_fields = ['source_points', 'destination_points', 'cargos']
  readonly_fields = ['discord_message_id', 'base_template']
  save_as = True
  actions = ['create_job_from_template']
  list_filter = ['template', 'cargos']
  fieldsets = [
    (None, {
      "fields": [
        'name',
        'cargos',
        ('quantity_requested', 'quantity_fulfilled'),
        'source_points',
        'destination_points',
        'expired_at',
        'rp_mode',
      ]
    }),
    ("Payout", {
      "fields": ['bonus_multiplier', 'completion_bonus']
    }),
    ("Description", {
      "fields": ['description']
    }),
    ("Job Template", {
      "fields": ['template', 'expected_player_count_for_quantity', 'job_posting_probability', 'template_job_period_hours', 'base_template']
    }),
    ("Discord integration", {
      "fields": ['discord_message_id']
    }),
  ]

  @admin.display(boolean=True)
  def finished(self, job):
    return job.fulfilled

  @admin.display(boolean=True)
  def postable(self, job):
    return async_to_sync(job.is_postable)()

  @admin.display()
  def num_posted(self, job):
    return job.num_posted

  def get_queryset(self, request):
    qs = super().get_queryset(request)
    return (qs
      .annotate_active()
      .prefetch_related('source_points', 'destination_points', 'cargos')
      .annotate(
        num_posted=Count('job_postings', distinct=True)
      )
    )

  @admin.action(description="Create job from template")
  def create_job_from_template(self, request, queryset):
    created = 0
    for job in queryset.prefetch_related('cargos', 'source_points', 'destination_points').filter(template=True):
      new_job = DeliveryJob.objects.create(
        name=job.name,
        cargo_key=job.cargo_key,
        quantity_requested=job.quantity_requested,
        expired_at=timezone.now() + timedelta(hours=5),
        bonus_multiplier=job.bonus_multiplier,
        completion_bonus=job.completion_bonus,
        description=job.description,
      )
      new_job.cargos.add(*job.cargos.all())
      new_job.source_points.add(*job.source_points.all())
      new_job.destination_points.add(*job.destination_points.all())
      created += 1

    self.message_user(
      request,
      ngettext(
        "%d jobs was successfully created.",
        "%d jobs were successfully created.",
        created,
      )
      % created,
      messages.SUCCESS,
    )


@admin.register(Delivery)
class DeliveryAdmin(admin.ModelAdmin):
  list_display = ['id', 'character', 'cargo_key', 'quantity', 'sender_point', 'destination_point', 'job', 'timestamp']
  list_select_related = ['character', 'character__player', 'sender_point', 'destination_point', 'job']
  ordering = ['-timestamp']
  search_fields = ['cargo_key', 'character__name', 'sender_point__name', 'destination_point__name']
  autocomplete_fields = ['sender_point', 'destination_point', 'character', 'job']

@admin.register(ServerStatus)
class ServerStatusAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'fps', 'used_memory']

@admin.register(PlayerShift)
class PlayerShiftAdmin(admin.ModelAdmin):
  list_display = ['player', 'start_time_utc', 'end_time_utc', 'user_timezone']

@admin.register(RescueRequest)
class RescueRequestAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'character']
  list_select_related = ['character']
  autocomplete_fields = ['character', 'responders']

