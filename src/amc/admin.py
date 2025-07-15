from django.contrib import admin
from .models import (
  Player,
  Character,
  Vehicle,
  Company,
  PlayerChatLog,
  PlayerVehicleLog,
  PlayerStatusLog,
  ServerLog,
  BotInvocationLog,
  SongRequestLog,
)

class CharacterInlineAdmin(admin.TabularInline):
  model = Character
  readonly_fields = ['name']

@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
  list_display = ['unique_id']
  search_fields = ['unique_id', 'characters__name']
  inlines = [CharacterInlineAdmin]

class PlayerStatusLogInlineAdmin(admin.TabularInline):
  model = PlayerStatusLog
  readonly_fields = ['character', 'timespan', 'duration']
  exclude = ['original_log']

@admin.register(Character)
class CharacterAdmin(admin.ModelAdmin):
  list_display = ['name', 'player__unique_id', 'last_login', 'total_session_time']
  list_select_related = ['player']
  search_fields = ['player__unique_id', 'name']
  inlines = [PlayerStatusLogInlineAdmin]
  readonly_fields = ['player']

  @admin.display(ordering="last_login", boolean=False)
  def last_login(self, obj):
    return obj.last_login

  @admin.display(ordering="total_session_time", boolean=False)
  def total_session_time(self, obj):
    return obj.total_session_time

  def get_queryset(self, request):
    qs = super().get_queryset(request)
    return qs.with_last_login().with_total_session_time()

class PlayerVehicleLogInlineAdmin(admin.TabularInline):
  model = PlayerVehicleLog
  readonly_fields = ['character', 'vehicle']

@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
  list_display = ['id', 'name']
  search_fields = ['id', 'name']
  inlines = [PlayerVehicleLogInlineAdmin]

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

@admin.register(BotInvocationLog)
class BotInvocationLogAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'character', 'prompt']
  list_select_related = ['character', 'character__player']
  ordering = ['-timestamp']
  search_fields = ['character__name', 'character__player__unique_id']

@admin.register(SongRequestLog)
class SongRequestLogAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'character', 'song']
  list_select_related = ['character', 'character__player']
  ordering = ['-timestamp']
  search_fields = ['character__name', 'character__player__unique_id']

@admin.register(PlayerStatusLog)
class PlayerStatusLogAdmin(admin.ModelAdmin):
  list_display = ['character', 'timespan', 'duration']
  list_select_related = ['character', 'character__player']
  ordering = ['-timespan']
  readonly_fields = ['character', 'original_log']
  search_fields = ['character__name', 'character__player__unique_id']

@admin.register(PlayerVehicleLog)
class PlayerVehicleLogAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'character', 'vehicle', 'action']
  list_select_related = ['character', 'character__player', 'vehicle']
  ordering = ['-timestamp']
  search_fields = ['character__name', 'character__player__unique_id', 'vehicle__id']

@admin.register(ServerLog)
class ServerLogAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'text', 'event_processed']
  ordering = ['-timestamp']
  list_filter = ['event_processed']

