from django.contrib import admin
from .models import (
  Player,
  Character,
  PlayerChatLog,
  PlayerVehicleLog,
  PlayerStatusLog,
  ServerLog
)

@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
  list_display = ['unique_id']
  search_fields = ['unique_id', 'character__name']

@admin.register(Character)
class CharacterAdmin(admin.ModelAdmin):
  list_display = ['name', 'player__unique_id']
  list_select_related = ['player']
  search_fields = ['player__unique_id']

@admin.register(PlayerChatLog)
class PlayerChatLogAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'character', 'text']
  list_select_related = ['character', 'character__player']
  ordering = ['-timestamp']

@admin.register(PlayerStatusLog)
class PlayerStatusLogAdmin(admin.ModelAdmin):
  list_display = ['character', 'timespan']
  list_select_related = ['character', 'character__player']
  ordering = ['-timespan']

@admin.register(PlayerVehicleLog)
class PlayerVehicleLogAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'character', 'vehicle', 'action']
  list_select_related = ['character', 'character__player', 'vehicle']
  ordering = ['-timestamp']

@admin.register(ServerLog)
class ServerLogAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'text']
  ordering = ['-timestamp']

