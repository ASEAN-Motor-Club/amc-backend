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
from django.contrib.postgres.aggregates import ArrayAgg
from django.template.response import TemplateResponse
from typing import cast, Any, TYPE_CHECKING
if TYPE_CHECKING:
    from .models import CharacterQuerySet, DeliveryJobQuerySet
from .models import (
  TeamMembership,
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
  CharacterVehicle,
  Garage,
  WorldText,
  WorldObject,
  SubsidyArea,
  SubsidyRule,
  DeliveryJobTemplate,
  MinistryElection,
  MinistryCandidacy,
  MinistryVote,
  MinistryTerm,
  MinistryDashboard,
)
from amc_finance.services import send_fund_to_player
from amc_finance.admin import AccountInlineAdmin
from amc.dashboard_services import get_ministry_dashboard_stats
from .widgets import AMCOpenLayersWidget
from django.urls import path
from django.http import HttpResponse
from django.shortcuts import render


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
    qs = cast("CharacterQuerySet", super().get_queryset(request))
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
  model = TeamMembership
  autocomplete_fields = ['player', 'character']
  show_change_link = True

class PlayerTeamInlineAdmin(admin.TabularInline):
  model = TeamMembership
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
    qs = cast("CharacterQuerySet", super().get_queryset(request))
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
  ordering = [cast(Any, F('timespan__startswith').desc(nulls_last=True))]
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

@admin.register(DeliveryJobTemplate)
class DeliveryJobTemplateAdmin(admin.ModelAdmin):
  list_display = ['name', 'default_quantity', 'completion_bonus', 'rp_mode', 'job_posting_probability']
  search_fields = ['name', 'description']
  filter_horizontal = ['cargos', 'source_points', 'destination_points']

@admin.register(DeliveryJob)
class DeliveryJobAdmin(admin.ModelAdmin):
  list_display = ['id', 'name', 'completion_bonus', 'finished', 'requested_at', 'postable']
  ordering = ['-requested_at']
  search_fields = [
    'name',
    'cargo_key',
    'cargos__label',
    'source_points__name',
    'destination_points__name',
  ]
  autocomplete_fields = ['source_points', 'destination_points', 'cargos']
  readonly_fields = ['discord_message_id', 'quantity_fulfilled', 'finished']
  save_as = True
  list_filter = ['cargos']
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
        'created_from',
      ]
    }),
    ("Payout", {
      "fields": ['bonus_multiplier', 'completion_bonus']
    }),
    ("Description", {
      "fields": ['description']
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

  def get_queryset(self, request):
    qs = cast("DeliveryJobQuerySet", super().get_queryset(request))
    return (qs
      .annotate_active()
      .prefetch_related('source_points', 'destination_points', 'cargos')
    )

  def add_view(self, request, form_url='', extra_context=None):
    if request.method == 'GET' and 'template' not in request.GET and 'scratch' not in request.GET:
        context = {
            **self.admin_site.each_context(request),
            'templates': DeliveryJobTemplate.objects.all(),
            'title': "Create Delivery Job",
        }
        return render(request, 'admin/amc/deliveryjob/select_template.html', context)

    return super().add_view(request, form_url, extra_context)

  def get_changeform_initial_data(self, request):
      initial = super().get_changeform_initial_data(request)
      if 'template' in request.GET:
          try:
              template = DeliveryJobTemplate.objects.get(pk=request.GET['template'])
              cast(dict[str, Any], initial).update({
                  'name': template.name,
                  'description': template.description,
                  'quantity_requested': template.default_quantity,
                  'bonus_multiplier': template.bonus_multiplier,
                  'completion_bonus': template.completion_bonus,
                  'rp_mode': template.rp_mode,
                  'created_from': template,
                  'cargos': template.cargos.all(),
                  'source_points': template.source_points.all(),
                  'destination_points': template.destination_points.all(),
                  'expired_at': timezone.now() + timedelta(hours=template.duration_hours),
              })
          except DeliveryJobTemplate.DoesNotExist:
              pass
      return initial



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

@admin.register(CharacterVehicle)
class CharacterVehicleAdmin(admin.ModelAdmin):
  list_display = ['id', 'character', 'company_guid', 'vehicle_id', 'vehicle_name', 'rental']
  list_select_related = ['character']
  autocomplete_fields = ['character']
  search_fields = ['company_guid', 'character__name', 'character__guid', 'character__player__unique_id']
  list_filter = ['rental', 'spawn_on_restart', 'for_sale']

  @admin.display()
  def vehicle_name(self, obj):
    return obj.config.get('VehicleName', '-')

@admin.register(Garage)
class GarageAdmin(admin.ModelAdmin):
  list_display = ['id', 'notes', 'spawn_on_restart']
  search_fields = ['notes']

@admin.register(WorldText)
class WorldTextAdmin(admin.ModelAdmin):
  list_display = ['id', 'content']

@admin.register(WorldObject)
class WorldObjectAdmin(admin.ModelAdmin):
  list_display = ['id', 'asset_path']


@admin.register(SubsidyArea)
class SubsidyAreaAdmin(admin.ModelAdmin):
    list_display = ['name']
    search_fields = ['name']

    class Media:
        js = (
            "https://cdn.jsdelivr.net/npm/ol@v7.2.2/dist/ol.js",
            "amc/js/OLMapWidget.js",
        )
        css = {
            "all": (
                "https://cdn.jsdelivr.net/npm/ol@v7.2.2/ol.css",
                "gis/css/ol3.css",
            )
        }

    def get_form(self, request, obj=None, change=False, **kwargs):
        defaults = {
            'widgets': {
                'polygon': AMCOpenLayersWidget,
            }
        }
        defaults.update(kwargs)
        return super().get_form(request, obj, change, **defaults)


@admin.register(SubsidyRule)
class SubsidyRuleAdmin(admin.ModelAdmin):
    list_display = ['name', 'active', 'priority', 'reward_type', 'reward_value']
    list_filter = ['active', 'reward_type', 'scales_with_damage', 'requires_on_time']
    search_fields = ['name']
    filter_horizontal = ['cargos', 'source_areas', 'destination_areas']
    autocomplete_fields = ['source_delivery_points', 'destination_delivery_points']
    change_list_template = 'admin/amc/subsidyrule/change_list.html'
    ordering = ['-priority']

    fieldsets = (
        (None, {
            'fields': ('name', 'active', 'priority', 'reward_type', 'reward_value', 'scales_with_damage', 'requires_on_time')
        }),
        ('Ministry Budget', {
            'fields': ('allocation', 'spent'),
            'description': "Track the Ministry's allocated budget for this subsidy."
        }),
        ('Requirements', {
            'fields': ('cargos',),
             'classes': ('collapse',),
        }),
        ('Source', {
            'fields': ('source_areas', 'source_delivery_points'),
            'classes': ('collapse',),
            'description': "Leave empty to apply to ANY source."
        }),
        ('Destination', {
             'fields': ('destination_areas', 'destination_delivery_points'),
             'classes': ('collapse',),
             'description': "Leave empty to apply to ANY destination."
        }),
    )

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('reorder/', self.admin_site.admin_view(self.reorder_view), name='subsidy-rule-reorder'),
        ]
        return custom_urls + urls

    def reorder_view(self, request):
        if request.method == "POST":
            ids = request.POST.getlist('ids[]')
            if not ids:
                # Fallback if HTMX/JS sends it differently (e.g. without [])
                ids = request.POST.getlist('ids')
            
            if not ids:
                 return HttpResponse("No IDs provided", status=400)
            
            # Higher priority = top of list
            # List comes in as [top_id, second_id, ...]
            # So top_id gets highest number.
            
            count = len(ids)
            for index, id in enumerate(ids):
                SubsidyRule.objects.filter(pk=id).update(priority=count - index)
                
            return HttpResponse("Ordered")
        return HttpResponse("Method not allowed", status=405)

@admin.register(MinistryElection)
class MinistryElectionAdmin(admin.ModelAdmin):
  list_display = ['id', 'created_at', 'candidacy_end_at', 'poll_end_at', 'winner', 'phase', 'is_processed']
  list_filter = ['winner', 'is_processed']

  def phase(self, obj):
    return obj.phase.label

@admin.register(MinistryCandidacy)
class MinistryCandidacyAdmin(admin.ModelAdmin):
  list_display = ['id', 'election', 'candidate', 'created_at']
  list_filter = ['election']
  search_fields = ['candidate__discord_name', 'candidate__unique_id']

@admin.register(MinistryVote)
class MinistryVoteAdmin(admin.ModelAdmin):
  list_display = ['id', 'election', 'voter', 'candidate', 'created_at']
  list_filter = ['election']

@admin.register(MinistryTerm)
class MinistryTermAdmin(admin.ModelAdmin):
  list_display = ['id', 'minister', 'start_date', 'end_date', 'initial_budget', 'current_budget', 'is_active']
  list_filter = ['is_active', 'minister']
  search_fields = ['minister__discord_name', 'minister__unique_id']
  readonly_fields = ['created_jobs_count', 'expired_jobs_count', 'total_spent']


@admin.register(MinistryDashboard)
class MinistryDashboardAdmin(admin.ModelAdmin):
    change_list_template = "admin/amc/ministrydashboard/change_list.html"

    def changelist_view(self, request, extra_context=None):
        # 1. Fetch current term
        timezone.now()
        current_term = MinistryTerm.objects.filter(is_active=True).first()

        # 2. Subsidies
        subsidies = list(SubsidyRule.objects.filter(active=True).order_by('-priority')[:5])
        for subsidy in subsidies:
            if subsidy.reward_type == SubsidyRule.RewardType.PERCENTAGE:
                subsidy.reward_percentage = int(subsidy.reward_value * 100)

        # 3. Recent Jobs
        # Jobs funded by this term (if exists) 
        if current_term:
            recent_jobs = list(DeliveryJob.objects.filter(funding_term=current_term).order_by('-requested_at')[:5])
            for job in recent_jobs:
                job.bonus_percentage = int(job.bonus_multiplier * 100)
        else:
            recent_jobs = []

        # 4. Charts Data
        stats = get_ministry_dashboard_stats(term=current_term, days=30)

        context = {
            **self.admin_site.each_context(request),
            'title': "Ministry of Commerce Dashboard",
            'ministry_term': current_term,
            'subsidies': subsidies,
            'recent_jobs': recent_jobs,
            'dashboard_stats': json.dumps(stats),
            **(extra_context or {}),
        }
        return TemplateResponse(request, cast(Any, self.change_list_template), context)
