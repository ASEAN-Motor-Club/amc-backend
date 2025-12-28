import logging
import asyncio
from datetime import timedelta
import discord
from discord import app_commands
from discord.ext import commands
from django.utils import timezone
from django.conf import settings
from django.db.models import Q, F, Sum, Count, Min
from django.contrib.gis.geos import Point
from .utils import create_player_autocomplete
from amc.models import Player, CharacterLocation, TeleportPoint, Ticket, PlayerMailMessage, Delivery, PlayerChatLog
from amc_finance.models import Account
from amc.mod_server import show_popup, teleport_player, get_player, transfer_money, list_player_vehicles
from amc.game_server import announce, is_player_online, kick_player, ban_player, get_players
from amc.vehicles import format_vehicle_name, format_vehicle_parts

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
  admin = app_commands.Group(
    name="admin",
    description="Admin-only commands",
    default_permissions=discord.Permissions(administrator=True)
  )

  admin_teleport = app_commands.Group(
    name="teleport",
    description="Teleport management commands",
    parent=admin
  )

  admin_vehicles = app_commands.Group(
    name="vehicles",
    description="Vehicle management commands",
    parent=admin
  )

  def __init__(self, bot):
    self.bot = bot
    self.logger = logging.getLogger(__name__)
    self.player_autocomplete = create_player_autocomplete(self.bot.http_client_game)

  async def player_autocomplete(self, interaction, current):
    return await self.player_autocomplete(interaction, current)

  @admin.command(name='announce', description='Sends an announcement')
  @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
  async def announce_in_game(self, ctx, message: str):
    await announce(message, self.bot.http_client_game)
    await ctx.response.send_message(f'Message sent: {message}', ephemeral=True)

  @admin.command(name='popup', description='Sends a popup message to an in-game player')
  @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
  @app_commands.autocomplete(player_id=player_autocomplete)
  async def send_popup(self, ctx, player_id: str, message: str):
    player = await Player.objects.aget(
      Q(unique_id=player_id) | Q(discord_user_id=player_id)
    )
    mail_message = f"""\
<Bold>Message from {ctx.user.display_name}</>

{message}
"""
    if await is_player_online(player_id, self.bot.http_client_game):
      await show_popup(self.bot.http_client_mod, mail_message, player_id=player.unique_id)
    else:
      await PlayerMailMessage.objects.acreate(
        to_player=player,
        content=mail_message
      )
    await ctx.response.send_message(f'Popup sent to {player.unique_id}: {message}')

  @admin_teleport.command(name='add', description='Create a new teleport point')
  @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
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

  @admin_teleport.command(name='remove', description='Remove a new teleport point')
  @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
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

  @admin_teleport.command(name='list', description='List all teleport points available to you')
  @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
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

  @admin_teleport.command(name='to', description='Teleport in-game')
  @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
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

  @admin_teleport.command(name='player', description='Teleport to a player')
  @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
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

  @admin.command(name='ticket', description='Sends a ticket to a player')
  @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
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

  @admin.command(name='transfer', description='Transfer money')
  @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
  @app_commands.autocomplete(player_id=player_autocomplete)
  async def transfer_money_cmd(self, ctx, player_id: str, amount: int, message: str):
    await transfer_money(self.bot.http_client_mod, amount, message, player_id)
    await ctx.response.send_message('Transfered')

  @admin.command(name='ban', description='Ban a player from the server')
  @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
  @app_commands.autocomplete(player_id=player_autocomplete)
  async def ban_player_cmd(self, ctx, player_id: str, hours: int=None, reason: str=''):
    player = await Player.objects.prefetch_related('characters').aget(
      Q(unique_id=player_id) | Q(discord_user_id=player_id)
    )
    character_names = ', '.join([
      c.name for c in player.characters.all()
    ])
    await ban_player(self.bot.http_client_game, player_id, hours, reason)
    await ban_player(self.bot.http_client_event, player_id, hours, reason)
    await ctx.response.send_message(f'Banned {player_id} (Aliases: {character_names}) for {hours} hours, due to: {reason}')

  @admin.command(name='kick', description='Kick a player from the server')
  @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
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
    await kick_player(self.bot.http_client_event, player_id)
    await interaction.response.send_message(f'Kicked {player_id} (Aliases: {character_names})')

  @admin.command(name='profile', description='Profile a player')
  @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
  @app_commands.autocomplete(player_id=player_autocomplete)
  async def profile_player(self, ctx, player_id: str):
    await ctx.response.defer()
    
    now = timezone.now()
    seven_days_ago = now - timedelta(days=7)
    
    try:
      player = await Player.objects.with_total_session_time().with_last_login().annotate(
        first_seen=Min('characters__status_logs__timespan__startswith'),
        recent_session_time=Sum(
          'characters__status_logs__duration',
          filter=Q(characters__status_logs__timespan__startswith__gte=seven_days_ago),
          default=timedelta(0)
        )
      ).aget(Q(unique_id=player_id) | Q(discord_user_id=player_id))
    except Player.DoesNotExist:
      await ctx.followup.send("Player not found")
      return

    # Aggregate stats separately to avoid join explosion in the main query
    economy = await Delivery.objects.filter(character__player=player).aaggregate(
      total_payment=Sum('payment', default=0),
      total_subsidy=Sum('subsidy', default=0),
      count=Count('id')
    )
    total_revenue = economy['total_payment'] + economy['total_subsidy']
    total_deliveries = economy['count']
    avg_payment = total_revenue / total_deliveries if total_deliveries > 0 else 0
    
    total_messages = await PlayerChatLog.objects.filter(character__player=player).acount()
    tickets_count = await player.tickets.acount()

    try:
        latest_char = await player.get_latest_character()
        display_name = latest_char.name
    except Exception:
        display_name = str(player.unique_id)

    embed = discord.Embed(
      title=f"Player Profile: {display_name}",
      color=discord.Color.blue(),
      timestamp=now
    )

    # Section 1: Identity
    verified_str = "✅ Verified" if player.verified else "❌ Unverified"
    flags = []
    if player.adminstrator:
        flags.append("🛡️ Admin")
    if player.suspect:
        flags.append("⚠️ Suspect")
    if player.displayer:
        flags.append("🎨 Displayer")
    flags_str = " | ".join(flags) if flags else "None"
    
    identity_val = (
        f"**ID:** `{player.unique_id}`\n"
        f"**Status:** {verified_str}\n"
    )
    if player.discord_user_id:
        identity_val += f"**Discord:** <@{player.discord_user_id}>"
        if player.discord_name:
            # Check if discord_name is different from mentions name (though we can't easily check here without fetching member)
            identity_val += f" (`{player.discord_name}`)"
        identity_val += "\n"
        
    identity_val += (
        f"**Social Score:** `{player.social_score}`\n"
        f"**Flags:** {flags_str}"
    )
    if player.notes:
        identity_val += f"\n**Notes:** {player.notes}"
    embed.add_field(name="👤 Identity", value=identity_val, inline=False)

    # Section 2: Characters (Alts)
    alts_lines = []
    chars = player.characters.all().with_last_login().order_by('-last_login')
    async for char in chars:
        lvls = (
            f"D:{char.driver_level or 0} | T:{char.truck_level or 0} | "
            f"Tx:{char.taxi_level or 0} | B:{char.bus_level or 0} | "
            f"P:{char.police_level or 0} | W:{char.wrecker_level or 0} | "
            f"R:{char.racer_level or 0}"
        )
        rp = " (RP)" if char.rp_mode else ""
        
        # Fetch bank balance from amc_finance.Account
        bank_balance = await Account.objects.filter(
            character=char, 
            book=Account.Book.BANK
        ).aaggregate(total=Sum('balance', default=0))
        bank_val = bank_balance['total']
        
        alts_lines.append(
            f"• **{char.name}**{rp}\n"
            f"  └ Wallet: `${char.money or 0:,}` | Bank: `${bank_val:,.0f}`\n"
            f"  └ Levels: `[{lvls}]`"
        )
    
    embed.add_field(name="👥 Characters (Alts)", value="\n".join(alts_lines) or "None", inline=False)

    # Section 3: Activity
    def format_duration(td):
        if not td:
            return "0s"
        total_seconds = int(td.total_seconds())
        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        if not parts:
            parts.append(f"{seconds}s")
        return " ".join(parts[:2]) # Show top 2 units

    first_seen_str = player.first_seen.strftime('%Y-%m-%d') if player.first_seen else "Never"
    last_login_str = player.last_login.strftime('%Y-%m-%d %H:%M') if player.last_login else "Never"
    activity_val = (
        f"**First seen:** {first_seen_str}\n"
        f"**Last online:** {last_login_str}\n"
        f"**Total online:** {format_duration(player.total_session_time)}\n"
        f"**Recent (7d):** {format_duration(player.recent_session_time)}"
    )
    embed.add_field(name="📊 Activity", value=activity_val, inline=True)

    # Section 4: Economy & Social
    economy_val = (
        f"**Deliveries:** `{total_deliveries}`\n"
        f"**Revenue:** `${total_revenue:,}`\n"
        f"**Avg/Job:** `${avg_payment:,.0f}`\n"
        f"**Messages:** `{total_messages}`"
    )
    embed.add_field(name="💰 Economy & Chat", value=economy_val, inline=True)

    # Section 5: Infractions
    infractions_val = f"**Total Tickets:** `{tickets_count}`"
    recent_tickets = player.tickets.all().order_by('-created_at')[:3]
    ticket_lines = []
    async for t in recent_tickets:
        ticket_lines.append(f"• {t.created_at.strftime('%Y-%m-%d')} - {t.get_infringement_display()}")
    
    if ticket_lines:
        infractions_val += "\n" + "\n".join(ticket_lines)
    embed.add_field(name="⚖️ Infractions", value=infractions_val, inline=False)

    # Section 6: Teams
    # Fetch team memberships asynchronously
    teams_list = []
    async for tm in player.team_memberships.all().select_related('team'):
        teams_list.append(tm.team.name)
        
    if teams_list:
        embed.add_field(name="🏢 Teams", value=", ".join(teams_list), inline=False)

    await ctx.followup.send(embed=embed)

  @admin_vehicles.command(name='all', description='List players spawned vehicles')
  @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
  async def list_players_vehicles_cmd(self, ctx):
    try:
      players = await get_players(self.bot.http_client_game)
    except Exception as e:
      self.logger.error(f"Failed to get players: {e}")
      players = []

    resp = "# Player Vehicles\n\n"
    for player_id, player_data in players:
      player_name = player_data['name']
      player_vehicles = await list_player_vehicles(self.bot.http_client_mod, player_id)

      resp += f"""
{player_name}: {len(player_vehicles)}"""
    await ctx.response.send_message(resp)

  @admin_vehicles.command(name='player', description='List a player\'s spawned vehicles')
  @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
  @app_commands.autocomplete(player_id=player_autocomplete)
  async def list_player_vehicles_cmd(self, ctx, player_id: str, only_active_vehicle: bool=True, include_trailers: bool=False):
    await ctx.response.defer()
    player = await Player.objects.prefetch_related('characters').aget(
      Q(unique_id=player_id) | Q(discord_user_id=player_id)
    )
    character = await player.get_latest_character()
    try:
      player_vehicles = await list_player_vehicles(self.bot.http_client_mod, player_id)
    except Exception:
      await ctx.followup.send(f"Failed to get {character.name}'s vehicles, make sure they are online")
      return

    if not player_vehicles:
      await ctx.followup.send(f"{character.name} has not spawned any vehicles")
      return

    if only_active_vehicle:
      player_vehicles = {v_id: v for v_id, v in player_vehicles.items() if v['isLastVehicle'] and (include_trailers or v['index'] == 0)}
      if not player_vehicles:
        await ctx.followup.send(f"{character.name} has no active vehicles")
        return

    for vehicle in player_vehicles.values():
      embed = discord.Embed(
        title=f"{character.name}'s {format_vehicle_name(vehicle['fullName'])} (#{vehicle['vehicleId']})",
        color=discord.Color.dark_grey(),
        description=format_vehicle_parts([p for p in vehicle['parts'] if p['Slot'] < 135]),
        timestamp=timezone.now()
      )
      await ctx.followup.send(embed=embed)

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


