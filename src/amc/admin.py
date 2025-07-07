from django.contrib import admin
from .models import Player, Character, PlayerChatLog, ServerLog

@admin.register(PlayerChatLog)
class PlayerChatLogAdmin(admin.ModelAdmin):
  list_display = ['character__name', 'text']

@admin.register(ServerLog)
class ServerLogAdmin(admin.ModelAdmin):
  list_display = ['timestamp', 'text']

