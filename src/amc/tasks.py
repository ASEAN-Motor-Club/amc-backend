import re
import asyncio
import discord
import random
from decimal import Decimal
from datetime import timedelta
from django.utils import timezone
from django.db import connection
from django.db.models import Exists, OuterRef, Q, F
from django.contrib.gis.geos import Point
from django.conf import settings
from django.core.signing import Signer
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
  ServerStartedLogEvent,
  UnknownLogEntry,
)
from amc.models import (
  Player,
  Character,
  CharacterLocation,
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
  TeleportPoint,
  VehicleDealership,
  Thank,
  Delivery,
  DeliveryJob,
  DeliveryPoint,
)
from amc.game_server import announce, get_players
from amc.mod_server import (
  show_popup, transfer_money, teleport_player, get_player,
)
from amc.auth import verify_player
from amc.mailbox import send_player_messages
from amc.events import (
  setup_event,
  show_scheduled_event_results_popup,
  staggered_start,
  auto_starting_grid,
)
from amc.locations import gwangjin_shortcut
from amc.utils import format_in_local_tz, format_timedelta, delay, get_time_difference_string
from amc.subsidies import DEFAULT_SAVING_RATE, SUBSIDIES_TEXT
from amc_finance.services import (
  register_player_withdrawal,
  register_player_take_loan,
  get_player_bank_balance,
  get_player_loan_balance,
  get_character_max_loan,
  calc_loan_fee,
  get_character_total_donations,
  player_donation,
)
from amc_finance.models import Account, LedgerEntry
from amc.webhook import on_player_profit


def get_welcome_message(last_login, player_name):
  if not last_login:
    return f"Welcome {player_name}! Use /help to see the available commands on this server. Join the discord at aseanmotorclub.com. Have fun!", True
  sec_since_login = (timezone.now() - last_login).seconds
  if sec_since_login > (3600 * 24 * 7):
    return f"Long time no see! Welcome back {player_name}", False
  if sec_since_login > 3600:
    return f"Welcome back {player_name}!", False
  return None, False


async def aget_or_create_character(player_name, player_id, http_client_mod=None):
  character_guid = None
  player_info = None
  if http_client_mod:
    while True:
      try:
        player_info = await get_player(http_client_mod, player_id)
        character_guid = player_info.get('CharacterGuid')
        if character_guid != Character.INVALID_GUID:
          break
        await asyncio.sleep(1)
      except Exception as e:
        print(f"Failed to fetch player info for {player_name} ({player_id}): {e}")
        break

  character, player, character_created, player_created = await Character.objects.aget_or_create_character_player(
    player_name,
    player_id,
    character_guid
  )
  return (character, player, character_created, player_info)


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
    await channel.send(
      discord.utils.escape_mentions(content),
      allowed_mentions=discord.AllowedMentions.none()
    )

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

async def process_log_event(event: LogEvent, http_client=None, http_client_mod=None, ctx = {}, hostname=''):
  discord_client = ctx.get('discord_client')
  timestamp = event.timestamp
  is_current_event = ctx.get('startup_time') and timestamp > ctx.get('startup_time')

  forward_message = None

  match event:
    case PlayerChatMessageLogEvent(timestamp, player_name, player_id, message):
      character, player, *_ = await aget_or_create_character(player_name, player_id, http_client_mod)
      await PlayerChatLog.objects.acreate(
        timestamp=timestamp,
        character=character, 
        text=message,
      )
      if command_match := re.match(r"/shortcutcheck", message):
        in_shortcut = await CharacterLocation.objects.filter(
          character=character,
          location__coveredby=gwangjin_shortcut,
          timestamp__gte=timestamp - timedelta(seconds=5),
        ).aexists()
        used_shortcut = await CharacterLocation.objects.filter(
          character=character,
          location__coveredby=gwangjin_shortcut,
          timestamp__gte=timestamp - timedelta(hours=1),
        ).aexists()
        popup_message = "<Title>Gwangjin Shortcut Status</>\n"
        if in_shortcut:
          popup_message += "<Warning>You are inside the forbidden zone</>\n"
        else:
          popup_message += "<EffectGood>You are outside the forbidden zone</>\n"

        if used_shortcut:
          popup_message += "<Warning>You were detected inside the forbidden zone in the last hour</>\n"
        else:
          popup_message += "<EffectGood>You have not been inside the forbidden zone for the last hour</>\n"
        asyncio.create_task(show_popup(http_client_mod, popup_message, player_id=str(player_id)))
      if command_match := re.match(r"/help$", message):
        asyncio.create_task(show_popup(http_client_mod, settings.HELP_TEXT, player_id=str(player_id)))
        await BotInvocationLog.objects.acreate(
          timestamp=timestamp,
          character=character, 
          prompt="help",
        )
      if command_match := re.match(r"/credits?$", message):
        asyncio.create_task(show_popup(http_client_mod, settings.CREDITS_TEXT, player_id=str(player_id)))
        await BotInvocationLog.objects.acreate(
          timestamp=timestamp,
          character=character, 
          prompt="credits",
        )
      if command_match := re.match(r"/jobs", message):
        jobs = DeliveryJob.objects.filter(
          quantity_fulfilled__lt=F('quantity_requested'),
          expired_at__gte=timestamp,
        ).prefetch_related('source_points', 'destination_points', 'cargos')

        def display_job(job):
          if job.cargo_key:
            cargo_key = job.get_cargo_key_display()
          else:
            cargo_key = ', '.join([
              cargo.label
              for cargo in job.cargos.all()
            ])
          title = f"""\
({job.quantity_fulfilled}/{job.quantity_requested}) {job.name} · <EffectGood>{job.bonus_multiplier*100:.0f}%</> · <Money>{job.completion_bonus:,}</> 
<Secondary>Expiring in {get_time_difference_string(timestamp, job.expired_at)}</>"""
          title += f'\n<Secondary>Cargo: {cargo_key}</>'
          source_points = list(job.source_points.all())
          if source_points:
            title += '\n<Secondary>'
            title += 'ONLY from: '
            title += ', '.join([point.name for point in source_points])
            title += '</>'
          destination_points = list(job.destination_points.all())
          if destination_points:
            title += '\n<Secondary>'
            title += 'ONLY to: '
            title += ', '.join([point.name for point in destination_points])
            title += '</>'
          if job.description:
            title += f'\n<Secondary>{job.description}</>'
          return title

        jobs_str = "\n\n".join([ display_job(job) async for job in jobs ])
        asyncio.create_task(show_popup(http_client_mod, f"""\
<Title>Delivery Jobs</>
<Secondary>Complete jobs solo or with other players and share the completion bonus!</>

{jobs_str}


<Title>Subsidies</>
<Secondary>These jobs are always subsidised on the server.</>

{SUBSIDIES_TEXT}
""", player_id=str(player_id)))

      if command_match := re.match(r"/subsidies", message):
        asyncio.create_task(show_popup(http_client_mod, SUBSIDIES_TEXT, player_id=str(player_id)))
        await BotInvocationLog.objects.acreate(
          timestamp=timestamp,
          character=character, 
          prompt="subsidies",
        )

      if command_match := re.match(r"/staggered_start\s*(?P<delay>\d+)$", message):
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
          asyncio.create_task(staggered_start(
            http_client,
            http_client_mod,
            active_event,
            player_id=player_id,
            delay=float(command_match.group('delay'))
          ))
        except Exception as e:
          asyncio.create_task(show_popup(http_client_mod, f"Failed: {e}", player_id=str(player_id)))

      if command_match := re.match(r"/auto_grid$", message):
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
          asyncio.create_task(auto_starting_grid(
            http_client_mod,
            active_event,
          ))
        except Exception as e:
          asyncio.create_task(show_popup(http_client_mod, f"Failed: {e}", player_id=str(player_id)))

      if command_match := re.match(r"/results", message):
        active_event = await ScheduledEvent.objects.filter_active_at(timestamp).select_related('race_setup').afirst()
        if not active_event:
          asyncio.create_task(show_popup(http_client_mod, "No active events", player_id=str(player_id)))
          return
        asyncio.create_task(show_scheduled_event_results_popup(http_client_mod, active_event, player_id=str(player_id)))
      if command_match := re.match(r"/setup_event\s*(?P<event_id>\d*)", message):
        try:
          if event_id := command_match.group('event_id'):
            scheduled_event = await (ScheduledEvent.objects
              .select_related('race_setup')
              .filter(
                race_setup__isnull=False
              )
              .aget(pk=int(event_id))
            )
          else:
            scheduled_event = await (ScheduledEvent.objects
              .filter_active_at(timestamp)
              .select_related('race_setup')
              .filter(
                race_setup__isnull=False
              )
              .afirst()
            )
            if not scheduled_event:
              asyncio.create_task(show_popup(http_client_mod, "There does not seem to be an active event. Please first create an event.", player_id=str(player_id)))
              return
          event_setup = await setup_event(timestamp, player_id, scheduled_event, http_client_mod)
          if event_setup:
            asyncio.create_task(show_popup(http_client_mod, "<Event>Event is setup!</>\n\nPress \"i\" to open the Event menu and start the race.\n\nYour times will be recorded automatically.\n\nGood luck!", player_id=str(player_id)))
          else:
            asyncio.create_task(show_popup(http_client_mod, "There does not seem to be an active event. Please first create an event.", player_id=str(player_id)))
        except Exception as e:
          asyncio.create_task(show_popup(http_client_mod, f"Failed to setup event: {e}", player_id=str(player_id)))
          raise e

        await BotInvocationLog.objects.acreate(
          timestamp=timestamp,
          character=character, 
          prompt="/setup_event",
        )
      elif command_match := re.match(r"/(coords|loc)$", message):
        player_info = await get_player(http_client_mod, str(player.unique_id))
        if player_info:
          location = player_info['Location']
          asyncio.create_task(
            announce(f"{int(float(location['X']))}, {int(float(location['Y']))}, {int(float(location['Z']))}", http_client, delay=0)
          )
      elif command_match := re.match(r"/(teleport|tp)\s+(?P<x>[-\d]+)\s+(?P<y>[-\d]+)\s+(?P<z>[-\d]+)$", message):
        player_info = await get_player(http_client_mod, str(player.unique_id))
        if player_info and player_info.get('bIsAdmin'):
          await teleport_player(
            http_client_mod,
            player.unique_id,
            {
              'X': int(command_match.group('x')), 
              'Y': int(command_match.group('y')), 
              'Z': int(command_match.group('z')),
            },
            no_vehicles=not player_info.get('bIsAdmin')
          )
      elif command_match := re.match(r"/(teleport|tp)\s+(?P<player_name>\S+)\s+(?P<tp_name>\S*)", message):
        player_name = command_match.group('player_name')
        tp_name = command_match.group('tp_name')

        players = await get_players(http_client)
        target_player_id = None
        for p_id, p_name in players:
          if player_name == p_name:
            target_player_id = p_id
            break
        if not target_player_id:
          asyncio.create_task(
            show_popup(http_client_mod, "<Title>Player not found</>", player_id=str(player_id))
          )
          return
        player_info = await get_player(http_client_mod, str(player.unique_id))
        if (not player_info or not player_info.get('bIsAdmin')):
          asyncio.create_task(
            show_popup(http_client_mod, "<Title>Admin-only Command</>", player_id=str(player_id))
          )
        else:
          if tp_name == "impound":
            location = {
              'X': -289988 + random.randint(-80_00, 80_00),
              'Y': 201790 + random.randint(-80_00, 80_00),
              'Z': -21950,
            }
          else:
            try:
              teleport_point = await TeleportPoint.objects.aget(
                Q(character=character) | Q(character__isnull=True),
                name__iexact=tp_name,
              )
              location = teleport_point.location
              location = {
                'X': location.x, 
                'Y': location.y, 
                'Z': location.z,
              }
            except TeleportPoint.DoesNotExist:
              asyncio.create_task(show_popup(http_client_mod, "Teleport point not found", player_id=str(player_id)))
              return
          await teleport_player(
            http_client_mod,
            target_player_id,
            location,
            no_vehicles=False
          )
          await BotInvocationLog.objects.acreate(
            timestamp=timestamp,
            character=character, 
            prompt=message,
          )

      elif command_match := re.match(r"/(teleport|tp)\s*(?P<name>.*)", message):
        name = command_match.group('name')
        player_info = await get_player(http_client_mod, str(player.unique_id))
        teleport_point_exists = await TeleportPoint.objects.filter(
          Q(character=character) | Q(character__isnull=True),
          name__iexact=name,
        ).aexists()
        if (not player_info or not player_info.get('bIsAdmin')) and (not name or not teleport_point_exists):
          tp_points = TeleportPoint.objects.filter(character__isnull=True).order_by('name')
          tp_points_names = [tp.name async for tp in tp_points]
          asyncio.create_task(
            show_popup(http_client_mod, f"<Title>Teleport</>\nUsage: <Highlight>/tp [location]</>\nChoose from one of the following locations:\n\n{'\n'.join(tp_points_names)}", player_id=str(player_id))
          )
        else:
          if name:
            try:
              teleport_point = await TeleportPoint.objects.aget(
                Q(character=character) | Q(character__isnull=True),
                name__iexact=name,
              )
              location = teleport_point.location
              location = {
                'X': location.x, 
                'Y': location.y, 
                'Z': location.z,
              }
            except TeleportPoint.DoesNotExist:
              asyncio.create_task(show_popup(http_client_mod, "Teleport point not found", player_id=str(player_id)))
              return
          else:
            location = player_info['CustomDestinationAbsoluteLocation']
            location['Z'] += 150
          await teleport_player(
              http_client_mod,
              player.unique_id,
              location,
              no_vehicles=not player_info.get('bIsAdmin')
            )
          await BotInvocationLog.objects.acreate(
            timestamp=timestamp,
            character=character, 
            prompt=message,
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
      if command_match := re.match(r"/events?", message):
        def get_event_start_time_in(event):
          if event.start_time > timezone.now():
            return f"{format_timedelta(event.start_time - timezone.now())} from now"
          return 'In progress'
        events_str = '\n\n'.join([
          f"""\
<Title>{event.name}</>
<Secondary>{format_in_local_tz(event.start_time)}</>
<Secondary>{get_event_start_time_in(event)}</>
{event.description}"""
          async for event in ScheduledEvent.objects.filter(end_time__gte=timezone.now()).order_by('start_time')
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
        if is_current_event:
          asyncio.create_task(
            show_popup(http_client_mod, "<Title>Your song is being downloaded</>\n\nThis usually takes 30-60 seconds.", player_id=str(player_id))
          )
      elif command_match := re.match(r"/set_saving_rate (?P<saving_rate>\d+)%?$", message):
        try:
          character.saving_rate = min(
            max(Decimal(command_match.group('saving_rate')) / Decimal(100), Decimal(0)),
            Decimal(1)
          )
          await character.asave(update_fields=['saving_rate'])
          asyncio.create_task(
            show_popup(http_client_mod, f"<Title>Savings rate saved</>\n\n{character.saving_rate*100:.0f}% of your earnings will automatically go into your bank account", player_id=str(player_id))
          )
        except Exception as e:
          asyncio.create_task(
            show_popup(http_client_mod, f"<Title>Set savings rate failed</>\n\n{e}", player_id=str(player_id))
          )

      elif command_match := re.match(r"/toggle_ubi$", message):
        try:
          character.reject_ubi = not character.reject_ubi
          await character.asave(update_fields=['reject_ubi'])
          if character.reject_ubi:
            popup_message = "You will no longer receive a universal basic income"
          else:
            popup_message = "You will start to receive a universal basic income"
          asyncio.create_task(
            show_popup(http_client_mod, popup_message, player_id=str(player_id))
          )
        except Exception as e:
          asyncio.create_task(
            show_popup(http_client_mod, f"<Title>Toggle UBI failed</>\n\n{e}", player_id=str(player_id))
          )

      elif command_match := re.match(r"/bank", message):
        balance = await get_player_bank_balance(character)
        loan_balance = await get_player_loan_balance(character)
        max_loan = get_character_max_loan(character, player)
        transactions = LedgerEntry.objects.filter(
          account__character=character,
          account__book=Account.Book.BANK,
        ).select_related('journal_entry').order_by('-journal_entry__created_at')[:10]
        transactions_str = '\n'.join([
          f"{tx.journal_entry.date} {tx.journal_entry.description:<25} <Money>{tx.credit - tx.debit:,}</>"
          async for tx in transactions
        ])
        saving_rate = character.saving_rate if character.saving_rate is not None else Decimal(DEFAULT_SAVING_RATE)
        asyncio.create_task(
          show_popup(http_client_mod, f"""\
<Title>Your Bank ASEAN Account</>

<Bold>Balance:</> <Money>{balance:,}</>
<Small>Daily (IRL) Interest Rate: 2.2% (offline), 4.4% (online).</>
<Bold>Loans:</> <Money>{loan_balance:,}</>
<Bold>Max Available Loan:</> <Money>{max_loan:,}</>
<Small>Max available loan depends on your driver level (currently {character.driver_level})</>
<Bold>Earnings Saving Rate:</> <Money>{saving_rate * Decimal(100):.0f}%</>
<Small>Use /set_saving_rate [percentage] to automatically set aside your earnings into your account.</>

Commands:
<Highlight>/set_saving_rate [percentage]</> - Automatically set aside your earnings into your account
<Highlight>/withdraw [amount]</> - Withdraw from your bank account
<Highlight>/loan [amount]</> - Take out a loan
<Highlight>/repay_loan [amount]</> - Repay your loan

How to Put Money in the Bank
<Secondary>Use the /set_saving_rate command to set how much you want to save. It's 0 by default.</>
<Secondary>You can only fill your bank account by saving your earnings on this server, not through direct deposits.</>

How ASEAN Loans Works
<Secondary>Our loans have a flat one-off 10% fee, and you only have to repay them when you make a profit.</>
<Secondary>The repayment will range from 10% to 80% of your income, depending on the amount of loan you took.</>

<Bold>Latest Transactions</>
{transactions_str}
""", player_id=str(player_id))
        )
      elif command_match := re.match(r"/donate\s+(?P<amount>[\d,]+)\s*(?P<verification_code>\S*)", message):
        max_donation = character.driver_level * 15_000 + character.truck_level * 15_000
        total_donations = await get_character_total_donations(
          character,
          timezone.now() - timedelta(days=7)
        )
        amount = int(command_match.group('amount').replace(',', ''))
        signer = Signer()
        signed_obj = signer.sign((amount, character.id))
        verification_code = signed_obj.replace('-', '').replace('_', '')[-4:]

        input_verification_code = command_match.group('verification_code')
        if not input_verification_code:
          asyncio.create_task(
            show_popup(http_client_mod, f"""\
<Title>Donation</>

~~ Thank you so much for your intention to donate ~~

To prevent any mishap, please read the following:
- Donations are <Bold>non-refundable</>
- Please do not donate more than your wallet balance! You will end up with negative balance.
- You may donate a maximum of {max_donation:,} per 7 days (irl)
- You have donated {total_donations:,} in the last 7 days (irl)

If you wish to proceed, type the command again followed by the verification code:
<Highlight>/donate {command_match.group('amount')} {verification_code.upper()}</>""", player_id=str(player_id))
          )
        elif input_verification_code.lower() != verification_code.lower():
          asyncio.create_task(
            show_popup(http_client_mod, f"""\
<Title>Donation</>

Sorry, the verification code did not match, please try again:
<Highlight>/donate {command_match.group('amount')} {verification_code.upper()}</>""", player_id=str(player_id))
          )
        else:
          try:
            amount = max(10_000, min(amount, max_donation - int(total_donations)))
            await player_donation(amount, character)
            await transfer_money(http_client_mod, int(-amount), 'Donation', player_id)
          except Exception as e:
            asyncio.create_task(
              show_popup(http_client_mod, f"<Title>Donation failed</>\n\n{e}", player_id=str(player_id))
            )
      elif command_match := re.match(r"/withdraw (?P<amount>\d+)", message):
        amount = int(command_match.group('amount'))
        balance = await get_player_bank_balance(character)
        amount = min(amount, balance)
        if amount > 0:
          try:
            await register_player_withdrawal(amount, character, player)
            await transfer_money(http_client_mod, int(amount), 'Bank Withdrawal', player_id)
          except Exception as e:
            asyncio.create_task(
              show_popup(http_client_mod, f"<Title>Withdrawal failed</>\n\n{e}", player_id=str(player_id))
            )
      elif command_match := re.match(r"/loan\s*(?P<amount>[\d,]+)\s*(?P<verification_code>\S*)", message):
        if not (await Delivery.objects.filter(character=character).aexists()):
          asyncio.create_task(
            announce("Loans are only for server residents", http_client)
          )
          return

        loan_balance = await get_player_loan_balance(character)
        max_loan = get_character_max_loan(character, player)
        amount = max(min(
          int(command_match.group('amount').replace(',', '')),
          max_loan - loan_balance
        ), 0)
        fee = calc_loan_fee(amount, character)

        signer = Signer()
        signed_obj = signer.sign((amount, character.id))
        verification_code = signed_obj.replace('-', '').replace('_', '')[-4:]

        input_verification_code = command_match.group('verification_code')
        if not input_verification_code:
          asyncio.create_task(
            show_popup(http_client_mod, f"""\
<Title>Taking out a loan</>

Based on your driving and trucking level, the fee for your loan would be:
<Money>{fee:,}</>

The total amount you will need to repay is:
<Money>{fee + amount:,}</>
<Secondary>A proportion of your earnings will be automatically deducted to repay this loan.</>

You will receive:
<Money>{amount:,}</>

If you wish to proceed, type the command again followed by the verification code:
<Highlight>/loan {command_match.group('amount')} {verification_code.upper()}</>""", player_id=str(player_id))
          )
        elif input_verification_code.lower() != verification_code.lower():
          asyncio.create_task(
            show_popup(http_client_mod, f"""\
<Title>Taking out a loan</>

Sorry, the verification code did not match, please try again:
<Highlight>/loan {command_match.group('amount')} {verification_code.upper()}</>""", player_id=str(player_id))
          )
        elif amount > 0:
          try:
            repay_amount, loan_fee = await register_player_take_loan(amount, character)
            await transfer_money(http_client_mod, int(amount), 'ASEAN Bank Loan', player_id)
            asyncio.create_task(
              show_popup(http_client_mod, f"""\
<Title>Loan Approved</>

Loan Approved!

Congratulations, your loan application was successful. Here is a summary of your new loan:

<Bold>Loan Amount Deposited:</> <Money>{amount:,}</>

<Bold>One-Time Loan Fee:</> <Money>{int(loan_fee):,}</>

<Bold>Total Balance to Repay:</> <Money>{int(repay_amount):,}</>

The loan amount has been deposited into your wallet. You can view your loan details and repayment schedule at any time from your account dashboard (<Highlight>/bank</>).""", player_id=str(player_id))
            )
          except Exception as e:
            asyncio.create_task(
              show_popup(http_client_mod, f"<Title>Loan failed</>\n\n{e}", player_id=str(player_id))
            )
      elif command_match := re.match(r"/repay_loan (?P<amount>\d+)", message):
        asyncio.create_task(
          show_popup(http_client_mod, "<Title>Command Removed</>\n\nYou will automatically repay your loan as you earn money on the server", player_id=str(player_id))
        )
        #amount = int(command_match.group('amount'))
        #loan_balance = await get_player_loan_balance(character)
        #amount = max(min(amount, loan_balance), 0)
        #if amount > 0:
        #  try:
        #    await register_player_repay_loan(amount, character)
        #    await transfer_money(http_client_mod, int(-amount), 'ASEAN Bank Loan Repayment', player_id)
        #  except Exception as e:
        #    asyncio.create_task(
        #      show_popup(http_client_mod, f"<Title>Loan failed</>\n\n{e}", player_id=str(player_id))
        #    )

      elif command_match := re.match(r"^/thank\s+(?P<player_name>\S+)$", message):
        player_name = command_match.group('player_name')
        if player_name == character.name:
          return

        players = await get_players(http_client)
        thanked_player_id = None
        for p_id, p_name in players:
          if player_name == p_name:
            thanked_player_id = p_id
            break
        if thanked_player_id is None:
          asyncio.create_task(
            show_popup(http_client_mod, "<Title>Player not found</>\n\nPlease make sure you typed the name correctly.", player_id=str(player_id))
          )
          raise Exception('Player not found')

        thanked_character = await Character.objects.select_related('player').filter(
          name=player_name,
          player__unique_id=int(thanked_player_id)
        ).alatest('status_logs__timespan__startswith')

        already_thanked = await Thank.objects.filter(
          sender_character=character,
          recipient_character=thanked_character,
          timestamp__gte=timestamp - timedelta(hours=1),
        ).aexists()

        if already_thanked:
          asyncio.create_task(
            show_popup(http_client_mod, "You have already thanked this player.\n\nYou may only thank a player once per hour.", player_id=str(player_id))
          )
        else:
          await Thank.objects.acreate(
            sender_character=character,
            recipient_character=thanked_character,
            timestamp=timestamp,
          )
          await Player.objects.filter(characters=thanked_character).aupdate(social_score=F('social_score')+1)
          asyncio.create_task(
            show_popup(http_client_mod, "<Title>Thank sent</>", player_id=str(player_id))
          )
          asyncio.create_task(
            show_popup(http_client_mod, f"<Title>{character.name} thanked you</>", player_id=str(thanked_player_id))
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
      character, *_ = await aget_or_create_character(player_name, player_id, http_client_mod)
      await PlayerVehicleLog.objects.acreate(
        timestamp=timestamp,
        character=character, 
        vehicle_game_id=vehicle_id,
        vehicle_name=vehicle_name,
        action=action,
      )
      if action == PlayerVehicleLog.Action.BOUGHT and vehicle_name == 'Vulcan':
        await player_donation(2_250_000, character)
      if discord_client and ctx.get('startup_time') and timestamp > ctx.get('startup_time'):
        forward_message = (
          settings.DISCORD_VEHICLE_LOGS_CHANNEL_ID,
          f"{player_name} ({player_id}) {action.label} vehicle: {vehicle_name} ({vehicle_id})"
        )

    case PlayerLoginLogEvent(timestamp, player_name, player_id):
      character, player, character_created, player_info = await aget_or_create_character(player_name, player_id, http_client_mod)
      if ctx.get('startup_time') and timestamp > ctx.get('startup_time'):
        try:
          last_login = None
          if not character_created:
            try:
              latest_status = await (PlayerStatusLog.objects
                .filter(character__player=player, timespan__endswith__isnull=False)
                .alatest('timespan__endswith')
              )
              last_login = latest_status.timespan.upper
            except PlayerStatusLog.DoesNotExist:
              pass
          welcome_message, is_new_player = get_welcome_message(last_login, player_name)
          if is_new_player:
            asyncio.create_task(
              show_popup(http_client_mod, settings.WELCOME_TEXT, player_id=str(player_id))
            )
          if welcome_message:
            asyncio.create_task(
              announce(welcome_message, http_client, delay=5)
            )
          if (is_new_player or player.suspect) and player_info.get('Location') is not None:
            location = Point(**{
              axis.lower(): value for axis, value in player_info.get('Location').items()
            })
            dps = DeliveryPoint.objects.filter(coord__isnull=False).only('coord')
            spawned_near_delivery_point = False
            async for dp in dps:
              if location.distance(dp.coord) < 200:
                spawned_near_delivery_point = True
                break

            if spawned_near_delivery_point:
              impound_location = {
                'X': -289988 + random.randint(-60_00, 60_00),
                'Y': 201790 + random.randint(-60_00, 60_00),
                'Z': -21950,
              }
              await teleport_player(
                http_client_mod,
                player.unique_id,
                impound_location,
                no_vehicles=False
              )
              asyncio.create_task(
                announce(f"{player_name}, you have been teleported since you spawned too close to a delivery point as a new player on the server.", http_client, color="FF0000")
              )
              player.suspect = True
              await player.asave(update_fields=['suspect'])
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
      character, *_ = await aget_or_create_character(player_name, player_id)
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
      character, *_ = await aget_or_create_character(owner_name, owner_id, http_client_mod)
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
      # TODO: skip if no client
      player_id = None
      if http_client:
        players = await get_players(http_client)
        for p_id, p_name in players:
          if player_name == p_name:
            player_id = p_id
            break
      if player_id is None:
        raise Exception('Player not found')

      character = await Character.objects.select_related('player').filter(
        name=player_name,
        player__unique_id=int(player_id)
      ).alatest('status_logs__timespan__startswith')
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
        subsidy_amount = 10_000
        await on_player_profit(
          character.player,
          subsidy_amount,
          subsidy_amount,
          http_client_mod
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

    case ServerStartedLogEvent(timestamp, _version):
      async def spawn_dealerships():
        async for vd in VehicleDealership.objects.filter(spawn_on_restart=True):
          await vd.spawn(http_client_mod)
      asyncio.create_task(
        delay(spawn_dealerships(), 60)
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

  await process_log_event(
    event, http_client=http_client, http_client_mod=http_client_mod, ctx=ctx, hostname=log.hostname
  )

  server_log.event_processed = True
  await server_log.asave(update_fields=['event_processed'])

  return {'status': 'created', 'timestamp': event.timestamp}

