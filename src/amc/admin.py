from django.contrib import admin
from django.utils import timezone
from django.db.models import F, Count
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


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
  list_display = ['unique_id', 'character_names', 'characters_count']
  search_fields = ['unique_id', 'characters__name']
  inlines = [CharacterInlineAdmin]

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
  search_fields = ['player__unique_id', 'name']
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
  ordering = ['rank', '-finished', 'disqualified', 'laps', 'section_index']

class LapSectionTimeInlineAdmin(admin.TabularInline):
  model = LapSectionTime

@admin.register(GameEvent)
class GameEventAdmin(admin.ModelAdmin):
  list_display = ['guid', 'name', 'start_time']
  inlines = [GameEventCharacterInlineAdmin]

@admin.register(GameEventCharacter)
class GameEventCharacterAdmin(admin.ModelAdmin):
  list_display = ['id', 'rank', 'character', 'game_event']
  inlines = [LapSectionTimeInlineAdmin]
  readonly_fields = ['character']
  search_fields = ['game_event__id']

@admin.register(CharacterLocation)
class CharacterLocationAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'character', 'location']
  readonly_fields = ['character']
