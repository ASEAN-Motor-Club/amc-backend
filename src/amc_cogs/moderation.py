import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from django.utils import timezone
from django.db.models import Q, F
from django.contrib.gis.geos import Point
from .utils import create_player_autocomplete
from amc.models import Player, CharacterLocation, TeleportPoint, Ticket, PlayerMailMessage
from amc.mod_server import show_popup, teleport_player, get_player, transfer_money, list_player_vehicles
from amc.game_server import announce, is_player_online, kick_player, ban_player, get_players

class VoteKickView(discord.ui.View):
  def __init__(self, player, player_id, bot, timeout=120):
    super().__init__(timeout=timeout)
    self.player = player
    self.player_id = player_id

    self.votes = {"yes": set(), "no": set()}
    self.vote_finished = asyncio.Event()
    self.bot = bot

  async def disable_buttons(self):
    for item in self.children:
        item.disabled = True
    await self.message.edit(view=self)

  async def on_timeout(self):
    await self.disable_buttons()
    self.vote_finished.set()

  async def finalize_vote(self):
    yes_count = len(self.votes["yes"])
    no_count = len(self.votes["no"])

    result = f"✅ Yes: {yes_count}\n❌ No: {no_count}\n\n"
    if yes_count > no_count and yes_count >= 3:
      result += f"🔨 Player **{self.player}** will be kicked!"
      await kick_player(self.bot.http_client_game, self.player_id)
    else:
      result += f"😇 Player **{self.player}** is safe."
      await announce(f'{self.player} survived the votekick', self.bot.http_client_game)

    await self.message.channel.send(result)

  @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
  async def yes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
    #member = interaction.guild.get_member(interaction.user.id)
    #if member and member.joined_at:
    #  now = datetime.utcnow()
    #  membership_duration = now - member.joined_at
    #  if membership_duration < timedelta(weeks=1):
    #    await interaction.response.send_message("You are not eligible to vote", ephemeral=True)
    #    return
    #else:
    #  await interaction.response.send_message("You are not eligible to vote", ephemeral=True)
    #  return

    self.votes["no"].discard(interaction.user.id)
    self.votes["yes"].add(interaction.user.id)
    await interaction.response.send_message("You voted ✅ Yes", ephemeral=True)

  @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
  async def no_button(self, interaction: discord.Interaction, button: discord.ui.Button):
    self.votes["yes"].discard(interaction.user.id)
    self.votes["no"].add(interaction.user.id)
    await interaction.response.send_message("You voted ❌ No", ephemeral=True)


class ModerationCog(commands.Cog):
  def __init__(self, bot):
    self.bot = bot
    self.player_autocomplete = create_player_autocomplete(self.bot.http_client_game)

  async def player_autocomplete(self, interaction, current):
    return await self.player_autocomplete(interaction, current)

  @app_commands.command(name='announce_in_game', description='Sends an announcement')
  @app_commands.checks.has_any_role(1395460420189421713)
  async def announce_in_game(self, ctx, message: str):
    await announce(message, self.bot.http_client_game)
    await ctx.response.send_message(f'Message sent: {message}', ephemeral=True)

  @app_commands.command(name='send_popup', description='Sends a popup message to an in-game player')
  @app_commands.checks.has_any_role(1395460420189421713)
  @app_commands.autocomplete(player_id=player_autocomplete)
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

  async def infringement_autocomplete(self, interaction, current):
    return [
      app_commands.Choice(name=label, value=key)
      for key, label in Ticket.Infringement.choices
      if current.lower() in label.lower()
    ]

  @app_commands.command(name='admin_ticket', description='Sends a ticket to a player')
  @app_commands.checks.has_any_role(1395460420189421713)
  @app_commands.autocomplete(player_id=player_autocomplete, infringement=infringement_autocomplete)
  async def ticket(self, interaction, player_id: str, infringement: str, message: str):
    try:
      admin = await Player.objects.aget(discord_user_id=interaction.user.id)
    except Player.DoesNotExist:
      await interaction.response.send_message('Please /verify yourself first')
      return

    await interaction.response.defer(ephemeral=True)

    player = await Player.objects.aget(
      Q(unique_id=player_id) | Q(discord_user_id=player_id)
    )
    character = await player.get_latest_character()
    new_ticket = await Ticket.objects.acreate(
      player=player,
      infringement=infringement,
      notes=message,
      issued_by=admin,
    )
    player.social_score = F('social_score') - Ticket.get_social_score_deduction(infringement)
    await player.asave(update_fields=['social_score'])

    mail_message = f"""\
<Bold>GOVERNMENT OF ASEAN MOTOR CLUB</>
<Bold>DEPARTMENT OF COMMUNITY STANDARDS & PUBLIC ORDER</>

<Title>OFFICIAL INFRINGEMENT NOTICE</>

<Bold>Case Number:</> {new_ticket.id}
<Bold>Date Issued:</> {new_ticket.created_at.strftime('%Y-%m-%d %H:%M:%S')}

<Bold>Infringement Category:</> {new_ticket.get_infringement_display()}

<Bold>Official's Notes:</>
{message}

---
This notice was issued by Officer {interaction.user.display_name}. If you wish to appeal this ticket, please contact a member of the administration team.

"""
    dm_success = False
    if await is_player_online(player_id, self.bot.http_client_game):
      await show_popup(self.bot.http_client_mod, mail_message, player_id=player_id)
      dm_success = True
    else:
      await PlayerMailMessage.objects.acreate(
        to_player=player,
        content=mail_message
      )

    embed = discord.Embed(
      title="**OFFICIAL INFRINGEMENT NOTICE**",
      color=discord.Color.red(),
      timestamp=timezone.now()
    )
    embed.set_author(name="ASEAN Motor Club | Department of Community Standards & Public Order")
    embed.add_field(name="Case Number", value=f"`{new_ticket.id}`", inline=True)
    embed.add_field(name="Date Issued", value=f"`{new_ticket.created_at.strftime('%Y-%m-%d %H:%M:%S')}`", inline=True)
    embed.add_field(name="Issued To", value=f"{character.name} (Player ID: `{player.unique_id})`", inline=False)
    embed.add_field(name="Infringement Category", value=new_ticket.get_infringement_display(), inline=False)
    embed.add_field(name="Official's Notes", value=f"```{message}```", inline=False)
    embed.set_footer(text=f"Issued by: {interaction.user.display_name}")

    # Send a copy to your private mod-log channel for record-keeping
    log_channel = self.bot.get_channel(1354451955774132284) 
    if log_channel:
      await log_channel.send(embed=embed)

    await announce(f"Citation issued to {character.name} for {new_ticket.get_infringement_display()}", self.bot.http_client_game, color="FF0000")

    # Confirm the action to the admin who ran the command
    if dm_success:
      await interaction.followup.send(f"Ticket `{new_ticket.id}` issued and sent to the player via popup.", embed=embed)
    else:
      await interaction.followup.send(f"Ticket `{new_ticket.id}` was created and a mail has been sent.", embed=embed)

  @app_commands.command(name='transfer_money', description='Transfer money')
  @app_commands.checks.has_any_role(1395460420189421713)
  @app_commands.autocomplete(player_id=player_autocomplete)
  async def transfer_money_cmd(self, ctx, player_id: str, amount: int, message: str):
    await transfer_money(self.bot.http_client_mod, amount, message, player_id)
    await ctx.response.send_message('Transfered')

  @app_commands.command(name='ban_player', description='Ban a player from the server')
  @app_commands.checks.has_any_role(1395460420189421713)
  @app_commands.autocomplete(player_id=player_autocomplete)
  async def ban_player_cmd(self, ctx, player_id: str, hours: int=None, reason: str=''):
    player = await Player.objects.prefetch_related('characters').aget(
      Q(unique_id=player_id) | Q(discord_user_id=player_id)
    )
    character_names = ', '.join([
      c.name for c in player.characters.all()
    ])
    await ban_player(self.bot.http_client_game, player_id, hours, reason)
    await ctx.response.send_message(f'Banned {player_id} (Aliases: {character_names}) for {hours} hours, due to: {reason}')

  @app_commands.command(name='kick_player', description='Kick a player from the server')
  @app_commands.checks.has_any_role(1395460420189421713)
  @app_commands.autocomplete(player_id=player_autocomplete)
  async def kick_player_cmd(self, interaction, player_id: str):
    player = await Player.objects.prefetch_related('characters').aget(
      Q(unique_id=player_id) | Q(discord_user_id=player_id)
    )
    character_names = ', '.join([
      c.name for c in player.characters.all()
    ])
    if not (await is_player_online(player_id, self.bot.http_client_game)):
      await interaction.response.send_message("Player not online", ephemeral=True)
      return

    await kick_player(self.bot.http_client_game, player_id)
    await interaction.response.send_message(f'Kicked {player_id} (Aliases: {character_names})')

  @app_commands.command(name='admin_profile_player', description='Profile a player')
  @app_commands.checks.has_any_role(1395460420189421713)
  @app_commands.autocomplete(player_id=player_autocomplete)
  async def profile_player(self, ctx, player_id: str):
    player = await Player.objects.prefetch_related('characters').aget(
      Q(unique_id=player_id) | Q(discord_user_id=player_id)
    )
    def character_report(char):
      return f"{char.name}"

    resp = f"""
# Player Report
#
{'\n\n'.join([character_report(c) async for c in player.characters.all()])}
"""
    await ctx.response.send_message(resp)

  @app_commands.command(name='admin_list_players_vehicles', description='List players spawned vehicles')
  @app_commands.checks.has_any_role(1395460420189421713)
  async def list_player_vehicles_cmd(self, ctx):
    try:
      players = await get_players(self.bot.http_client_game)
    except Exception as e:
      print(f"Failed to get players: {e}")
      players = []

    resp = "# Player Vehicles\n\n"
    for player_id, player_name in players:
      player_vehicles = await list_player_vehicles(self.bot.http_client_mod, player_id)

      resp += f"""
{player_name}: {len(player_vehicles)}"""
    await ctx.response.send_message(resp)

  @app_commands.command(name="votekick", description="Initiate a vote to kick a player")
  @app_commands.describe(player_id="The name of the player to kick")
  @app_commands.autocomplete(player_id=player_autocomplete)
  async def votekick(self, interaction: discord.Interaction, player_id: str):
    if interaction.channel.id != 1421915330279641098:
      await interaction.response.send_message("You can only use this command in the <#1421915330279641098> channel", ephemeral=True)

    #member = interaction.guild.get_member(interaction.user.id)
    #if member and member.joined_at:
    #  now = datetime.utcnow()
    #  membership_duration = now - member.joined_at
    #  if membership_duration < timedelta(weeks=1):
    #    await interaction.response.send_message("You are not eligible to vote. Joined less than a week ago", ephemeral=True)
    #    return
    #else:
    #  await interaction.response.send_message("You are not eligible to vote. Unknown member", ephemeral=True)
    #  return

    if not (await is_player_online(player_id, self.bot.http_client_game)):
      await interaction.response.send_message(
          "Player not found",
          ephemeral=True
      )
      return

    player = await Player.objects.prefetch_related('characters').aget(
      Q(unique_id=player_id) | Q(discord_user_id=player_id)
    )
    character = await player.get_latest_character()

    view = VoteKickView(character.name, player_id, self.bot)

    await announce(f'{interaction.user.display_name} initiated a votekick against {character.name}, vote within 120 seconds', self.bot.http_client_game)
    await interaction.response.send_message(
      f"🗳️ Vote to kick **{character.name}**!\nClick a button to vote. Abuse of this feature will not be tolerated. Voting ends in 120 seconds.",
      view=view
    )
    view.message = await interaction.original_response()
    await view.vote_finished.wait()
    await view.finalize_vote()


