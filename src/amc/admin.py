from django.contrib import admin
from .models import (
  Player,
  Character,
  Vehicle,
  Company,
  PlayerChatLog,
  PlayerVehicleLog,
  PlayerStatusLog,
  ServerLog
)

@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
  list_display = ['unique_id']
  search_fields = ['unique_id', 'character__name']

class PlayerStatusLogInlineAdmin(admin.TabularInline):
  model = PlayerStatusLog
  readonly_fields = ['character', 'original_log']
  exclude = ['original_log']

@admin.register(Character)
class CharacterAdmin(admin.ModelAdmin):
  list_display = ['name', 'player__unique_id']
  list_select_related = ['player']
  search_fields = ['player__unique_id']
  inlines = [PlayerStatusLogInlineAdmin]
  readonly_fields = ['player']

@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
  list_display = ['id', 'name']
  search_fields = ['id', 'name']

@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
  list_display = ['name', 'owner', 'is_corp', 'first_seen_at']
  list_filter =  ['is_corp']
  search_fields = ['owner__name', 'name']
  readonly_fields = ['owner']

@admin.register(PlayerChatLog)
class PlayerChatLogAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'character', 'text']
  list_select_related = ['character', 'character__player']
  ordering = ['-timestamp']

@admin.register(PlayerStatusLog)
class PlayerStatusLogAdmin(admin.ModelAdmin):
  list_display = ['character', 'timespan', 'duration']
  list_select_related = ['character', 'character__player']
  ordering = ['-timespan']
  readonly_fields = ['character', 'original_log']

@admin.register(PlayerVehicleLog)
class PlayerVehicleLogAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'character', 'vehicle', 'action']
  list_select_related = ['character', 'character__player', 'vehicle']
  ordering = ['-timestamp']

@admin.register(ServerLog)
class ServerLogAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'text', 'event_processed']
  ordering = ['-timestamp']

