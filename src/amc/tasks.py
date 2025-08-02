import re
import asyncio
import discord
from django.utils import timezone
from django.db import connection
from django.db.models import Exists, OuterRef
from django.conf import settings
from asgiref.sync import sync_to_async
from amc.models import ServerLog
from amc.server_logs import (
  parse_log_line,
  LogEvent,
  PlayerChatMessageLogEvent,
  PlayerRestockedDepotLogEvent,
  PlayerVehicleLogEvent,
  PlayerCreatedCompanyLogEvent,
  PlayerLevelChangedLogEvent,
  PlayerLoginLogEvent,
  LegacyPlayerLogoutLogEvent,
  PlayerLogoutLogEvent,
  CompanyAddedLogEvent,
  CompanyRemovedLogEvent,
  AnnouncementLogEvent,
  SecurityAlertLogEvent,
  UnknownLogEntry,
)
from amc.models import (
  Player,
  Character,
  PlayerStatusLog,
  PlayerChatLog,
  PlayerVehicleLog,
  PlayerRestockDepotLog,
  BotInvocationLog,
  SongRequestLog,
  Company,
  ScheduledEvent,
  GameEventCharacter,
  GameEvent,
)
from amc.game_server import announce
from amc.mod_server import show_popup
from amc.auth import verify_player
from amc.mailbox import send_player_messages
from amc.events import (
  setup_event,
  show_scheduled_event_results_popup,
  staggered_start,
)
from amc.utils import format_in_local_tz


def get_welcome_message(last_login, player_name):
  if not last_login:
    return f"Welcome {player_name}! Use /help to see the available commands on this server. Join the discord at aseanmotorclub.com. Have fun!"
  sec_since_login = (timezone.now() - last_login).seconds
  if sec_since_login > (3600 * 24 * 7):
    return f"Long time no see! Welcome back {player_name}"
  if sec_since_login > 3600:
    return f"Welcome back {player_name}!"
  return None


async def aget_or_create_character(player_name, player_id):
  player, _ = await Player.objects.aget_or_create(unique_id=player_id)
  character, character_created = await (Character.objects
    .with_last_login()
    .aget_or_create(player=player, name=player_name)
  )
  return (character, player, character_created)


async def process_login_event(character_id, timestamp):
  """Use CTE to update and insert to the PlayerStatusLog table at the same time
  to prevent race condition"""
  raw_sql = """
    WITH original_row AS (
      SELECT id, timespan, lower(timespan) as login_time
      FROM amc_playerstatuslog
      WHERE character_id = %(character_id)s AND timespan @> %(timestamp)s
      ORDER BY UPPER(timespan) ASC
      LIMIT 1
    ),
    updated_row AS (
      UPDATE amc_playerstatuslog
      SET timespan = tstzrange(%(timestamp)s, upper(timespan), '[)')
      WHERE id = (
        SELECT id from original_row
      )
    )
    INSERT INTO amc_playerstatuslog (character_id, timespan)
    SELECT
      %(character_id)s,
      tstzrange(
        (
          CASE WHEN exists (SELECT 1 FROM original_row)
          THEN (SELECT login_time FROM original_row)
          ELSE %(timestamp)s
          END
        ),
        NULL,
        '[)'
      )
      WHERE NOT exists (SELECT 1 from original_row WHERE login_time is null)
    ;
  """
  params = {
    "character_id": character_id,
    "timestamp": timestamp,
  }
  def _execute_raw_sql(sql, params):
    with connection.cursor() as cursor:
      cursor.execute(sql, params)

  async_execute_raw_sql = sync_to_async(
    _execute_raw_sql, 
    thread_sensitive=True # Important for database connections!
  )
  await async_execute_raw_sql(raw_sql, params)


async def process_logout_event(character_id, timestamp):
  """Use CTE to update and insert to the PlayerStatusLog table at the same time
  to prevent race condition"""
  raw_sql = """
    WITH original_row AS (
      SELECT id, timespan, upper(timespan) as logout_time
      FROM amc_playerstatuslog
      WHERE character_id = %(character_id)s AND timespan @> %(timestamp)s
      ORDER BY LOWER(timespan) DESC
      LIMIT 1
    ),
    updated_row AS (
      UPDATE amc_playerstatuslog
      SET timespan = tstzrange(lower(timespan), %(timestamp)s, '[)')
      WHERE id = (
        SELECT id from original_row
      )
    )
    INSERT INTO amc_playerstatuslog (character_id, timespan)
    SELECT
      %(character_id)s,
      tstzrange(
        NULL,
        (
          CASE WHEN exists (SELECT 1 FROM original_row)
          THEN (SELECT logout_time FROM original_row)
          ELSE %(timestamp)s
          END
        ),
        '[)'
      )
      WHERE NOT exists (SELECT 1 from original_row WHERE logout_time is null)
    ;
  """
  params = {
    "character_id": character_id,
    "timestamp": timestamp,
  }
  def _execute_raw_sql(sql, params):
    with connection.cursor() as cursor:
      cursor.execute(sql, params)

  async_execute_raw_sql = sync_to_async(
    _execute_raw_sql, 
    thread_sensitive=True # Important for database connections!
  )
  await async_execute_raw_sql(raw_sql, params)


async def forward_to_discord(client, channel_id, content):
  if not client.is_ready():
    await client.wait_until_ready()

  channel = client.get_channel(int(channel_id))
  if channel:
    await channel.send(content, allowed_mentions=discord.AllowedMentions.none())

async def add_discord_verified_role(client, discord_user_id, player_id):
  guild = client.get_guild(settings.DISCORD_GUILD_ID)
  if not guild:
    raise Exception("Could not find a guild with that ID.")

  member = guild.get_member(discord_user_id)
  if not member:
    raise Exception("Could not find a member with that ID.")

  # Get the role object from the role ID
  role = guild.get_role(settings.DISCORD_VERIFIED_ROLE_ID)
  if not role:
    raise Exception("Could not find a role with that ID.")

  await member.add_roles(role, reason=f"Action performed by {player_id}")

async def countdown(http_client, start=5, delay=1.0):
  await announce('Get ready', http_client)
  for i in range(start, -1, -1):
    await asyncio.sleep(delay)
    await announce(str(i) if i > 0 else 'GO!!', http_client, clear_banner=False)

async def process_log_event(event: LogEvent, http_client=None, http_client_mod=None, ctx = {}):
  discord_client = ctx.get('discord_client')

  forward_message = None

  match event:
    case PlayerChatMessageLogEvent(timestamp, player_name, player_id, message):
      character, player, _ = await aget_or_create_character(player_name, player_id)
      await PlayerChatLog.objects.acreate(
        timestamp=timestamp,
        character=character, 
        text=message,
      )
      if command_match := re.match(r"/help", message):
        asyncio.create_task(show_popup(http_client_mod, settings.HELP_TEXT, player_id=str(player_id)))
        await BotInvocationLog.objects.acreate(
          timestamp=timestamp,
          character=character, 
          prompt="help",
        )
      if command_match := re.match(r"/staggered_start (?P<delay>\d+)", message):
        active_event = await (GameEvent.objects
          .filter(
            Exists(GameEventCharacter.objects.filter(
              game_event=OuterRef('pk'),
              character=character
            ))
          )
          .select_related('race_setup')
          .alatest('last_updated')
        )
        if not active_event:
          asyncio.create_task(show_popup(http_client_mod, "No active events", player_id=str(player_id)))
        try:
          await staggered_start(
            http_client,
            http_client_mod,
            active_event,
            player_id=player_id,
            delay=float(command_match.group('delay'))
          )
        except Exception as e:
          asyncio.create_task(show_popup(http_client_mod, f"Failed: {e}", player_id=str(player_id)))
      if command_match := re.match(r"/results", message):
        active_event = await ScheduledEvent.objects.filter_active_at(timestamp).select_related('race_setup').afirst()
        if not active_event:
          asyncio.create_task(show_popup(http_client_mod, "No active events", player_id=str(player_id)))
          return
        asyncio.create_task(show_scheduled_event_results_popup(http_client_mod, active_event, player_id=str(player_id)))
      if command_match := re.match(r"/setup_event", message):
        try:
          event_setup = await setup_event(timestamp, player_id, http_client_mod)
          if event_setup:
            asyncio.create_task(show_popup(http_client_mod, "Event is setup!\n\nPlease join it and start the race.\n\nYour times will be recorded automatically.", player_id=str(player_id)))
          else:
            asyncio.create_task(show_popup(http_client_mod, "There does not seem to be an active event. Please first create an event.", player_id=str(player_id)))
        except Exception as e:
          asyncio.create_task(show_popup(http_client_mod, f"Failed to setup event: {e}", player_id=str(player_id)))

        await BotInvocationLog.objects.acreate(
          timestamp=timestamp,
          character=character, 
          prompt="/setup_event",
        )
      if command_match := re.match(r"/verify (?P<signed_message>.+)", message):
        try:
          discord_user_id = await verify_player(player, command_match.group('signed_message'))
          asyncio.run_coroutine_threadsafe(
            add_discord_verified_role(
              discord_client,
              discord_user_id,
              player_id
            ),
            discord_client.loop
          )
          asyncio.create_task(show_popup(http_client_mod, "You are now verified!", player_id=str(player_id)))
        except Exception as e:
          asyncio.create_task(show_popup(http_client_mod, f"Failed to verify: {e}", player_id=str(player_id)))
        await BotInvocationLog.objects.acreate(
          timestamp=timestamp,
          character=character, 
          prompt=f"verify {command_match.group('signed_message')}",
        )
      if command_match := re.match(r"/events", message):
        events_str = '\n\n'.join([
          f"""\
## {event.name}
{format_in_local_tz(event.start_time)}
{event.description}"""
          async for event in ScheduledEvent.objects.filter(end_time__gte=timezone.now())
        ])
        asyncio.create_task(
          show_popup(http_client_mod, f"[EVENTS]\n\n{events_str}", player_id=str(player_id))
        )
        await BotInvocationLog.objects.acreate(
          timestamp=timestamp,
          character=character, 
          prompt='/events',
        )
      if command_match := re.match(r"/countdown", message):
        asyncio.create_task(countdown(http_client))
        await BotInvocationLog.objects.acreate(
          timestamp=timestamp,
          character=character, 
          prompt='/countdown',
        )
      if command_match := re.match(r"/bot (?P<prompt>.+)", message):
        await BotInvocationLog.objects.acreate(
          timestamp=timestamp,
          character=character, 
          prompt=command_match.group('prompt'),
        )
      elif command_match := re.match(r"/song.request (?P<song>.+)", message):
        await SongRequestLog.objects.acreate(
          timestamp=timestamp,
          character=character, 
          song=command_match.group('song'),
        )
      if discord_client and ctx.get('startup_time') and timestamp > ctx.get('startup_time'):
        forward_message = (
          settings.DISCORD_GAME_CHAT_CHANNEL_ID,
          f"**{player_name}:** {message}"
        )

    case AnnouncementLogEvent(timestamp, message):
      if discord_client and ctx.get('startup_time') and timestamp > ctx.get('startup_time'):
        forward_message = (
          settings.DISCORD_GAME_CHAT_CHANNEL_ID,
          f"📢 {message}"
        )

    case PlayerVehicleLogEvent(timestamp, player_name, player_id, vehicle_name, vehicle_id):
      action = PlayerVehicleLog.action_for_event(event)
      character, _, _ = await aget_or_create_character(player_name, player_id)
      await PlayerVehicleLog.objects.acreate(
        timestamp=timestamp,
        character=character, 
        vehicle_game_id=vehicle_id,
        vehicle_name=vehicle_name,
        action=action,
      )
      if discord_client and ctx.get('startup_time') and timestamp > ctx.get('startup_time'):
        forward_message = (
          settings.DISCORD_VEHICLE_LOGS_CHANNEL_ID,
          f"{player_name} ({player_id}) {action.label} vehicle: {vehicle_name} ({vehicle_id})"
        )

    case PlayerLoginLogEvent(timestamp, player_name, player_id):
      character, player, character_created = await aget_or_create_character(player_name, player_id)
      if ctx.get('startup_time') and timestamp > ctx.get('startup_time'):
        try:
          last_login = character.last_login if not character_created else None
          welcome_message = get_welcome_message(last_login, player_name)
          if welcome_message:
            asyncio.create_task(
              announce(welcome_message, http_client, delay=5)
            )
        except Exception as e:
          asyncio.create_task(
            announce(f'Failed to greet player: {e}', http_client)
          )
      await process_login_event(character.id, timestamp)
      asyncio.create_task(send_player_messages(http_client_mod, player))
      if discord_client and ctx.get('startup_time') and timestamp > ctx.get('startup_time'):
        forward_message = (
          settings.DISCORD_GAME_CHAT_CHANNEL_ID,
          f"**🟢 Player Login:** {player_name} ({player_id})"
        )

    case PlayerLogoutLogEvent(timestamp, player_name, player_id):
      character, _, _ = await aget_or_create_character(player_name, player_id)
      await process_logout_event(character.id, timestamp)
      if discord_client and ctx.get('startup_time') and timestamp > ctx.get('startup_time'):
        forward_message = (
          settings.DISCORD_GAME_CHAT_CHANNEL_ID,
          f"**🔴 Player Logout:** {player_name} ({player_id})"
        )

    case LegacyPlayerLogoutLogEvent(timestamp, player_name):
      character = await Character.objects.aget(
        Exists(
          PlayerStatusLog.objects.filter(
            character=OuterRef('pk'),
            timespan__upper_inf=True
          )
        ),
        name=player_name,
      )
      await process_logout_event(character.id, timestamp)

    case CompanyAddedLogEvent(timestamp, company_name, is_corp, owner_name, owner_id) | CompanyRemovedLogEvent(timestamp, company_name, is_corp, owner_name, owner_id):
      character, _, _ = await aget_or_create_character(owner_name, owner_id)
      company, company_created = await Company.objects.aget_or_create(
        name=company_name,
        owner=character,
        is_corp=is_corp,
        defaults={
          'first_seen_at': timestamp
        }
      )
      if company_created and is_corp:
        # Announce license requirements
        pass

    case PlayerRestockedDepotLogEvent(timestamp, player_name, depot_name):
      character = await Character.objects.aget(
        name=player_name,
      )
      await PlayerRestockDepotLog.objects.acreate(
        timestamp=timestamp,
        character=character,
        depot_name=depot_name,
      )
      if discord_client and ctx.get('startup_time') and timestamp > ctx.get('startup_time'):
        forward_message = (
          settings.DISCORD_GAME_CHAT_CHANNEL_ID,
          f"**📦 Player Restocked Depot:** {player_name} (Depot: {depot_name})"
        )

    case PlayerCreatedCompanyLogEvent(timestamp, player_name, company_name):
      # Handled by CompanyAddedLogEvent, if created
      pass

    case PlayerLevelChangedLogEvent(timestamp, player_name, player_id, level_type, level_value):
      match level_type:
        case 'CL_Driver':
          field_name = 'driver_level'
        case 'CL_Bus':
          field_name = 'bus_level'
        case 'CL_Taxi':
          field_name = 'taxi_level'
        case 'CL_Police':
          field_name = 'police_level'
        case 'CL_Truck':
          field_name = 'truck_level'
        case 'CL_Wrecker':
          field_name = 'wrecker_level'
        case 'CL_Racer':
          field_name = 'racer_level'
        case _:
          raise ValueError('Unknown level type')
      await Character.objects.filter(name=player_name, player__unique_id=player_id).aupdate(
        **{field_name: level_value}
      )

    case UnknownLogEntry():
      raise ValueError('Unknown log entry')
    case SecurityAlertLogEvent():
      pass
    case _:
      pass

  if forward_message and discord_client and ctx.get('startup_time') and timestamp > ctx.get('startup_time'):
    forward_message_channel_id, forward_message_content = forward_message
    asyncio.run_coroutine_threadsafe(
      forward_to_discord(
        discord_client,
        forward_message_channel_id,
        forward_message_content
      ),
      discord_client.loop
    )

async def process_log_line(ctx, line):
  log, event = parse_log_line(line)
  server_log, server_log_created = await ServerLog.objects.aget_or_create(
    timestamp=log.timestamp,
    hostname=log.hostname,
    tag=log.tag,
    text=log.content,
    log_path=log.log_path,
  )
  if not server_log_created and server_log.event_processed:
    return {'status': 'duplicate', 'timestamp': event.timestamp}

  # TODO rename context variable names
  # Separate main server and event server sessions
  match log.hostname:
    case 'asean-mt-server':
      http_client = ctx.get('http_client')
      http_client_mod = ctx.get('http_client_mod')
    case 'motortown-server-event':
      http_client = ctx.get('http_client_event')
      http_client_mod = ctx.get('http_client_event_mod')
    case _:
      http_client = ctx.get('http_client')
      http_client_mod = ctx.get('http_client_mod')

  await process_log_event(event, http_client=http_client, http_client_mod=http_client_mod, ctx=ctx)

  server_log.event_processed = True
  await server_log.asave(update_fields=['event_processed'])

  return {'status': 'created', 'timestamp': event.timestamp}

