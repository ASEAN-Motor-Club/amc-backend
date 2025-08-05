from django.contrib import admin
from django.utils import timezone
from django.db.models import F, Count, Window
from django.db.models.functions import RowNumber
from django.contrib.postgres.aggregates import ArrayAgg
from .models import (
  Player,
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
  DeliveryPoint,
)

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


class TeamPlayerInlineAdmin(admin.TabularInline):
  model = Team.players.through
  autocomplete_fields = ['player']
  show_change_link = True

class PlayerTeamInlineAdmin(admin.TabularInline):
  model = Player.teams.through
  show_change_link = True

@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
  list_display = ['unique_id', 'character_names', 'characters_count', 'discord_user_id', 'verified']
  search_fields = ['unique_id', 'characters__name', 'discord_user_id']
  autocomplete_fields = ['user']
  inlines = [CharacterInlineAdmin, PlayerTeamInlineAdmin]

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
  search_fields = ['player__unique_id', 'player__discord_user_id', 'name']
  inlines = [PlayerStatusLogInlineAdmin]
  readonly_fields = ['player', 'last_login', 'total_session_time']

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
  list_display = ['timestamp', 'text', 'event_processed']
  ordering = ['-timestamp']
  list_filter = ['event_processed']
  search_fields = ['text']


class GameEventCharacterInlineAdmin(admin.TabularInline):
  model = GameEventCharacter
  readonly_fields = ['character']
  show_change_link = True

class LapSectionTimeInlineAdmin(admin.TabularInline):
  model = LapSectionTime

@admin.register(GameEvent)
class GameEventAdmin(admin.ModelAdmin):
  list_display = ['guid', 'name', 'start_time', 'scheduled_event']
  inlines = [GameEventCharacterInlineAdmin]

@admin.register(GameEventCharacter)
class GameEventCharacterAdmin(admin.ModelAdmin):
  list_display = ['id', 'rank', 'character', 'finished', 'net_time', 'game_event', 'game_event__scheduled_event', 'game_event__last_updated']
  list_select_related = ['character', 'character__player', 'game_event', 'game_event__scheduled_event']
  inlines = [LapSectionTimeInlineAdmin]
  readonly_fields = ['character']
  search_fields = ['game_event__id', 'game_event__scheduled_event__name', 'character__name', 'game_event__race_setup__hash']
  list_filter = ['finished', 'game_event__scheduled_event']
  actions = ['award_event_points']
  ordering = ['-game_event__last_updated', 'net_time']

  @admin.action(description="Award event points")
  def award_event_points(self, request, queryset):
    championship = Championship.objects.last()
    # TODO: Create custom ParticipantQuerySet
    participants = queryset.filter(finished=True).annotate(
      p_rank=Window(
        expression=RowNumber(),
        partition_by=[F('character')],
        order_by=[F('net_time').asc()]
      ),
      time_trial=F('game_event__scheduled_event__time_trial')
    ).filter(
      p_rank=1
    ).order_by('net_time')
    cps = [
      ChampionshipPoint(
        championship=championship,
        participant=participant,
        team=participant.character.player.teams.last(),
        points=ChampionshipPoint.get_event_points_for_position(i, time_trial=participant.time_trial)
      )
      for i, participant in enumerate(participants)
    ]
    ChampionshipPoint.objects.bulk_create(cps)


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

@admin.register(ChampionshipPoint)
class ChampionshipPointAdmin(admin.ModelAdmin):
  list_display = ['championship', 'participant__character', 'participant__game_event__scheduled_event__name', 'team', 'points']
  list_select_related = ['championship', 'participant__character', 'team', 'participant__game_event__scheduled_event']
  search_fields = ['championship__name']

@admin.register(ScheduledEvent)
class ScheduledEventAdmin(admin.ModelAdmin):
  list_display = ['name', 'race_setup', 'start_time', 'discord_event_id']
  list_select_related = ['race_setup']
  inlines = [GameEventInlineAdmin]
  search_fields = ['name', 'race_setup__hash', 'race_setup__name']
  autocomplete_fields = ['race_setup']

@admin.register(RaceSetup)
class RaceSetupAdmin(admin.ModelAdmin):
  list_display = ['hash', 'route_name', 'num_laps', 'vehicles', 'engines']
  search_fields = ['hash', 'name']
  inlines = [GameEventInlineAdmin]

@admin.register(CharacterLocation)
class CharacterLocationAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'character', 'location']
  readonly_fields = ['character']
  search_fields = ['character__name', 'character__player__unique_id']

@admin.register(PlayerMailMessage)
class PlayerMailMessageAdmin(admin.ModelAdmin):
  list_select_related = ['to_player', 'from_player']
  list_display = ['sent_at', 'to_player', 'received_at', 'content']
  autocomplete_fields = ['to_player', 'from_player']


@admin.register(DeliveryPoint)
class DeliveryPointAdmin(admin.ModelAdmin):
  list_display = ['guid', 'name', 'coord', 'last_updated']
  search_fields = ['name', 'guid']

