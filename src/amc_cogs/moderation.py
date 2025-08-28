from discord import app_commands
from discord.ext import commands
from django.db.models import Q
from django.contrib.gis.geos import Point
from .utils import create_player_autocomplete
from amc.models import Player, CharacterLocation, TeleportPoint
from amc.mod_server import show_popup, teleport_player, get_player

class ModerationCog(commands.Cog):
  def __init__(self, bot):
    self.bot = bot
    self.player_autocomplete = create_player_autocomplete(self.bot.event_http_client_game)

  async def player_autocomplete(self, interaction, current):
    return await self.player_autocomplete(interaction, current)

  @app_commands.command(name='send_popup', description='Sends a popup message to an in-game player')
  @app_commands.checks.has_any_role(1395460420189421713)
  async def send_popup(self, ctx, player_id: str, message: str):
    player = await Player.objects.aget(
      Q(unique_id=player_id) | Q(discord_user_id=player_id)
    )
    await show_popup(self.bot.http_client_mod, message, player_id=player.unique_id)
    await ctx.response.send_message(f'Popup sent to {player.unique_id}: {message}')

  @app_commands.command(name='add_teleport_point', description='Create a new teleport point')
  @app_commands.checks.has_any_role(1395460420189421713)
  async def add_teleport_point(self, ctx, name: str):
    try:
      player = await Player.objects.aget(discord_user_id=ctx.user.id)
      character = await player.get_latest_character()
      player_info_main = await get_player(self.bot.http_client_mod, str(player.unique_id))
      player_info_event = await get_player(self.bot.event_http_client_mod, str(player.unique_id))
      player_info = player_info_main or player_info_event
      if not player_info:
        await ctx.response.send_message('You don\'t seem to be logged in')
        return
      location = player_info.get('CustomDestinationAbsoluteLocation')
      location = Point(location['X'], location['Y'], location['Z'])
      await TeleportPoint.objects.acreate(
        character=character,
        location=location,
        name=name,
      )
      await ctx.response.send_message(f'New teleport point {name} created at {location.x:.0f}, {location.y:.0f}, {location.z:.0f}')
    except Player.DoesNotExist:
      await ctx.response.send_message('Please /verify yourself first')
    except Exception as e:
      await ctx.response.send_message(f'Failed to create new teleport point: {e}')

  @app_commands.command(name='remove_teleport_point', description='Remove a new teleport point')
  @app_commands.checks.has_any_role(1395460420189421713)
  async def remove_teleport_point(self, ctx, name: str):
    try:
      player = await Player.objects.aget(discord_user_id=ctx.user.id)
      character = await player.get_latest_character()
      await TeleportPoint.objects.filter(
        character=character,
        name=name,
      ).adelete()
      await ctx.response.send_message(f'Removed teleport point {name}')
    except Player.DoesNotExist:
      await ctx.response.send_message('Please /verify yourself first')
    except Exception as e:
      await ctx.response.send_message(f'Failed to remove new teleport point: {e}')

  @app_commands.command(name='list_teleport_points', description='List all teleport points available to you')
  @app_commands.checks.has_any_role(1395460420189421713)
  async def list_teleport_points(self, ctx):
    try:
      player = await Player.objects.aget(discord_user_id=ctx.user.id)
      character = await player.get_latest_character()
      teleport_points = TeleportPoint.objects.select_related('character').filter(
        Q(character=character) | Q(character__isnull=True),
      )
      teleport_points_str = '\n'.join([
        f"{tp.name} ({tp.location.x}, {tp.location.y}, {tp.location.z}) {'**Global**' if not tp.character else ''}"
        async for tp in teleport_points
      ])
      await ctx.response.send_message(f'## Available teleport points:\n{teleport_points_str}', ephemeral=True)
    except Player.DoesNotExist:
      await ctx.response.send_message('Please /verify yourself first')
    except Exception as e:
      await ctx.response.send_message(f'Failed to list teleport points: {e}')

  async def teleport_name_autocomplete(self, interaction, current):
    player = await Player.objects.aget(discord_user_id=interaction.user.id)
    character = await player.get_latest_character()
    teleport_points = TeleportPoint.objects.filter(
      Q(character=character) | Q(character__isnull=True),
    )
    if current:
      teleport_points = teleport_points.filter(name__contains=current)

    return [
      app_commands.Choice(name=tp.name, value=tp.name)
      async for tp in teleport_points
    ]

  @app_commands.command(name='teleport', description='Teleport in-game')
  @app_commands.checks.has_any_role(1395460420189421713)
  @app_commands.autocomplete(name=teleport_name_autocomplete)
  async def teleport(self, ctx, name: str):
    try:
      player = await Player.objects.aget(discord_user_id=ctx.user.id)
      character = await player.get_latest_character()
      teleport_point = await TeleportPoint.objects.aget(
        Q(character=character) | Q(character__isnull=True),
        name=name,
      )
      location = teleport_point.location
      await teleport_player(self.bot.event_http_client_mod, str(player.unique_id), {
        'X': location.x, 
        'Y': location.y, 
        'Z': location.z,
      })
      await ctx.response.send_message(f'Teleported {character.name} to point {name} ({location.x:.0f}, {location.y:.0f}, {location.z:.0f})')
    except Player.DoesNotExist:
      await ctx.response.send_message('Please /verify yourself first')
    except Exception as e:
      await ctx.response.send_message(f'Failed to teleport: {e}')

  @app_commands.command(name='teleport_to_player', description='Teleport to a player')
  @app_commands.checks.has_any_role(1395460420189421713)
  @app_commands.autocomplete(player_id=player_autocomplete)
  async def teleport_to_player(self, ctx, player_id: str):
    try:
      player = await Player.objects.aget(discord_user_id=ctx.user.id)
      target_player = await Player.objects.aget(unique_id=int(player_id))
      target_character = await target_player.get_latest_character()
      target_character_location = await CharacterLocation.objects.fiter(
        character=target_character,
      ).alatest('timestamp')
      location = target_character_location.location
      await teleport_player(self.bot.http_client_event_mod, player.unique_id, {
        'X': location.x, 
        'Y': location.y, 
        'Z': location.z,
      })
      await ctx.response.send_message(f'Teleported to {target_character.name} ({location.x:.0f}, {location.y:.0f}, {location.z:.0f})')
    except Player.DoesNotExist:
      await ctx.response.send_message('Please /verify yourself first')
    except Exception as e:
      await ctx.response.send_message(f'Failed to teleport: {e}')

