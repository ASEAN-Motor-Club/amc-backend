import asyncio
import random
from django.utils import timezone
from django.db import connection
from django.db.models import Exists, OuterRef
from django.contrib.gis.geos import Point
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
    ServerStartedLogEvent,
    UnknownLogEntry,
)
from amc.models import (
    Team,
    Character,
    PlayerStatusLog,
    PlayerChatLog,
    PlayerVehicleLog,
    PlayerRestockDepotLog,
    Company,
    VehicleDealership,
    DeliveryPoint,
    CharacterVehicle,
    Garage,
    WorldText,
    WorldObject,
    NewsItem,
    CriminalRecord,
    FactionMembership,
    PoliceSession,
)
from amc.game_server import announce, get_players
from amc.utils import forward_to_discord
from amc.mod_server import (
    show_popup,
    teleport_player,
    get_player,
    list_player_vehicles,
    set_world_vehicle_decal,
    spawn_assets,
    spawn_garage,
)
from amc.mailbox import send_player_messages
from amc.utils import (
    delay,
)
from amc_finance.services import (
    player_donation,
    get_player_loan_balance,
)
from amc.mod_detection import detect_custom_parts, detect_incompatible_parts, POLICE_DUTY_WHITELIST
from amc.player_tags import refresh_player_name
from amc.webhook import on_player_profit
from amc.enums import VehicleKeyByLabel, VEHICLE_DATA
from amc.subsidies import repay_loan_for_profit
from amc.vehicles import spawn_registered_vehicle
import logging
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from amc.discord_client import AMCDiscordBot

logger = logging.getLogger(__name__)

# Discord message queue for ordered, non-blocking forwarding
_discord_queue: deque[tuple[str, str, float]] = (
    deque()
)  # (channel_id, content, timestamp)
_discord_client_ref: "AMCDiscordBot | None" = None  # Store reference to Discord client


def _process_discord_queue():
    """Process Discord messages in FIFO order. Called from the arq event loop."""
    global _discord_client_ref
    if not _discord_client_ref or not _discord_client_ref.loop:
        return

    while _discord_queue:
        channel_id, content, _ts = _discord_queue.popleft()
        try:
            asyncio.run_coroutine_threadsafe(
                forward_to_discord(_discord_client_ref, channel_id, content),
                _discord_client_ref.loop,
            )
        except Exception as e:
            logger.exception(f"Discord forward failed: {e}")


def enqueue_discord_message(channel_id: str, content: str, timestamp):
    """Non-blocking enqueue for Discord messages."""
    _discord_queue.append((channel_id, content, timestamp))
    # Process immediately since we're using run_coroutine_threadsafe
    _process_discord_queue()


async def _show_police_popup(http_client_mod, character_guid, player_id):
    """Show police rules popup with a wanted list of online characters with active criminal records."""
    try:
        rules = """\
<Title>Police Rules</>
To begin your police shift, type <Highlight>/police</> in chat.
This will activate your [Pn] tag and enable police commands.

<Bold>Commands (while on duty)</>
- <Highlight>/arrest</> — Arrest nearby suspects
- <Highlight>/tp vehicle</> — Teleport to your police car
- <Highlight>/police</> — End your shift

<Bold>Rules</>
- Ramming and spike strips are allowed against suspected criminals <Highlight>[C]</>
- <Warning>No ramming or spike strips against non-criminals without consent</>
- Communicate with other players before conducting chases

<Bold>Discord</Bold>
Use <Highlight>/faction</Highlight> on Discord to join the Police faction and gain access to the police-only channel."""

        # Get online players from mod server API
        from amc.mod_server import get_players as get_players_mod

        now = timezone.now()
        active_records = CriminalRecord.objects.filter(
            expires_at__gt=now
        ).select_related("character")

        # Filter to online characters only
        online_players = await get_players_mod(http_client_mod)
        if online_players:
            online_guids = {
                p.get("CharacterGuid", "").upper()
                for p in online_players
                if p.get("CharacterGuid")
            }
            active_records = active_records.filter(
                character__guid__in=online_guids
            )

        wanted_lines = []
        async for record in active_records:
            days_left = (record.expires_at - now).days
            wanted_lines.append(
                f"- {record.character.name} ({record.reason}) — expires in {days_left}d"
            )

        if wanted_lines:
            rules += "\n\n<Bold>Wanted List</>\n" + "\n".join(wanted_lines)

        await show_popup(http_client_mod, rules, character_guid=character_guid, player_id=player_id)
    except Exception as e:
        logger.exception(f"Failed to show police popup: {e}")


async def on_vehicle_sold(character, vehicle_name, http_client_mod):
    """Auto-repay loan from vehicle sale proceeds (50% of vehicle cost)."""
    try:
        vehicle_key = VehicleKeyByLabel.get(vehicle_name)
        if not vehicle_key:
            logger.debug(
                f"Vehicle '{vehicle_name}' not in VehicleKeyByLabel, skipping sale repayment"
            )
            return

        vehicle_data = VEHICLE_DATA.get(vehicle_key)
        if not vehicle_data:
            logger.debug(
                f"Vehicle key '{vehicle_key}' not in VEHICLE_DATA, skipping sale repayment"
            )
            return

        sale_proceeds = vehicle_data["cost"] // 2
        if sale_proceeds <= 0:
            return

        # Eagerly load the player relation to avoid SynchronousOnlyOperation
        # when repay_loan_for_profit accesses character.player.unique_id
        character = await Character.objects.select_related("player").aget(pk=character.pk)

        loan_balance = await get_player_loan_balance(character)
        if loan_balance <= 0:
            return

        await repay_loan_for_profit(character, sale_proceeds, http_client_mod)
        logger.info(
            f"Auto loan repayment from vehicle sale: {character.name} sold {vehicle_name} (proceeds: {sale_proceeds})"
        )
    except Exception as e:
        logger.exception(
            f"Vehicle sale loan repayment failed for {character.name}: {e}"
        )


def get_welcome_message(player_name, is_new, last_online=None):
    if is_new:
        return (
            f"Welcome {player_name}! Use /help to see the available commands on this server. Join the discord at aseanmotorclub.com. Have fun!",
            True,
        )
    if not last_online:
        # Existing player but last_online not yet populated — generic greeting
        return f"Welcome back {player_name}!", False
    sec_since_online = (timezone.now() - last_online).total_seconds()
    if sec_since_online > (3600 * 24 * 7):
        return f"Long time no see! Welcome back {player_name}", False
    if sec_since_online > 3600:
        return f"Welcome back {player_name}!", False
    return None, False


async def aget_or_create_character(player_name, player_id, http_client_mod=None):
    character_guid = None
    player_info = None
    if http_client_mod:
        # Single attempt — never blocks with retries
        try:
            player_info = await get_player(http_client_mod, player_id)
            if player_info:
                character_guid = player_info.get("CharacterGuid")
                # Mod server returns all-zeros GUID during early login — treat as absent
                if character_guid == Character.INVALID_GUID:
                    character_guid = None
        except Exception as e:
            logger.debug(f"Player info fetch failed (non-blocking): {e}")

    (
        character,
        player,
        character_created,
        player_created,
    ) = await Character.objects.aget_or_create_character_player(
        player_name, player_id, character_guid
    )
    return (character, player, character_created, player_info)


async def _resolve_guid(http_client_mod, player_id, player_name, max_attempts=20):
    """Retry GUID resolution from the mod server. Returns (character_guid, player_info) or (None, None)."""
    for i in range(max_attempts):
        try:
            player_info = await get_player(http_client_mod, player_id)
            if player_info:
                guid = player_info.get("CharacterGuid")
                if guid and guid != Character.INVALID_GUID:
                    return guid, player_info
            await asyncio.sleep(min(1 + i, 5))
        except Exception as e:
            logger.exception(
                f"Failed to fetch player info for {player_name} ({player_id}): {e}"
            )
            return None, None
    logger.warning(
        f"GUID not resolved after {max_attempts} attempts for {player_name} ({player_id})"
    )
    return None, None


async def _login_guid_dependent_actions(
    character,
    player,
    player_name,
    player_id,
    http_client,
    http_client_mod,
    character_created,
):
    """Fire-and-forget: GUID-dependent login side-effects that must not block the arq worker."""
    try:
        character_guid, player_info = await _resolve_guid(
            http_client_mod, player_id, player_name
        )
        if not character_guid:
            logger.warning(
                f"Skipping GUID-dependent login actions for {player_name} — GUID unresolved"
            )
            return

        # Persist GUID if newly resolved
        if not character.guid or character.guid != character_guid:
            character.guid = character_guid
            await character.asave(update_fields=["guid"])

        # --- Tag Enforcement ---
        # 1. Update the player's name based on current DB state
        await refresh_player_name(character, http_client_mod)

        # 2. Check if they tried to login with unauthorized tags and warn them
        if player_info:
            player_display_name = player_info.get("PlayerName", "")

            # DOT tag check
            if (
                "DOT" in player_display_name
                and not await Team.objects.filter(tag="DOT", players=player).aexists()
            ):
                asyncio.create_task(
                    show_popup(
                        http_client_mod,
                        "You are not authorised to use the DOT tag. It has been removed from your name.",
                        character_guid=character_guid,
                        player_id=str(player.unique_id),
                    )
                )

            # GOV tag check (for expired/non-employees trying to use the tag)
            import re

            if (
                re.search(r"\[GOV\d*\]", player_display_name, re.IGNORECASE)
                and not character.is_gov_employee
            ):
                asyncio.create_task(
                    show_popup(
                        http_client_mod,
                        "The [GOV] tag is reserved for government employees. It has been removed from your name.",
                        character_guid=character_guid,
                        player_id=str(player.unique_id),
                    )
                )

        # --- Welcome popup for new players ---
        if character_created:
            asyncio.create_task(
                show_popup(
                    http_client_mod,
                    settings.WELCOME_TEXT,
                    character_guid=character_guid,
                    player_id=str(player.unique_id),
                )
            )

        # --- News popup ---
        news_items = await NewsItem.aget_active()
        if news_items:
            from amc.commands.news import format_news_popup

            asyncio.create_task(
                show_popup(
                    http_client_mod,
                    format_news_popup(news_items),
                    character_guid=character_guid,
                    player_id=str(player.unique_id),
                )
            )

        # --- New player / suspect teleport check ---
        if (
            (character_created or player.suspect)
            and player_info
            and player_info.get("Location") is not None
            and player_info.get("VehicleKey") != "None"
        ):
            loc_data = player_info.get("Location")
            if loc_data:
                location = Point(
                    **{axis.lower(): value for axis, value in loc_data.items()}
                )
                dps = DeliveryPoint.objects.filter(coord__isnull=False).only("coord")
                spawned_near_delivery_point = False
                async for dp in dps:
                    if location.distance(dp.coord) < 400:
                        spawned_near_delivery_point = True
                        break
            else:
                spawned_near_delivery_point = False

            if spawned_near_delivery_point:
                impound_location = {
                    "X": -289988 + random.randint(-60_00, 60_00),
                    "Y": 201790 + random.randint(-60_00, 60_00),
                    "Z": -21950,
                }
                await teleport_player(
                    http_client_mod,
                    player.unique_id,
                    impound_location,
                    no_vehicles=False,
                )
                asyncio.create_task(
                    announce(
                        f"{player_name}, you have been teleported since you spawned too close to a delivery point as a new player on the server.",
                        http_client,
                        color="FF0000",
                    )
                )
                player.suspect = True
                await player.asave(update_fields=["suspect"])
    except Exception as e:
        logger.exception(f"GUID-dependent login actions failed for {player_name}: {e}")


async def register_player_vehicles(session, character, player):
    try:
        await list_player_vehicles(
            session, str(player.unique_id), active=True, complete=True
        )
        # TODO save to db?
    except Exception as e:
        logger.error(f"Failed to register player vehicles for {character.name}: {e}")


async def handle_player_vehicle_mod_check(
    character, player, session, action: PlayerVehicleLog.Action
):
    """Check modded parts when entering a vehicle, or remove MOD tag when exiting."""
    # When exiting, we just clear the [MODS] tag
    if action == PlayerVehicleLog.Action.EXITED:
        await refresh_player_name(character, session, has_custom_parts=False)
        return

    # When entering, we must fetch their active vehicle to see if it has custom parts
    if action == PlayerVehicleLog.Action.ENTERED:
        try:
            player_vehicles = await list_player_vehicles(
                session, str(player.unique_id), active=True, complete=True
            )
        except Exception as e:
            logger.error(f"Failed to fetch vehicle parts for {character.name}: {e}")
            return

        if not player_vehicles:
            # They entered a vehicle but list_player_vehicles returned empty?
            # Fallback: remove the tag
            await refresh_player_name(character, session, has_custom_parts=False)
            return

        # Check the first (main) active vehicle
        main_vehicle = next(
            (
                v
                for v in player_vehicles.values()
                if v.get("isLastVehicle") and v.get("index", -1) == 0
            ),
            None,
        )

        if not main_vehicle:
            # Fallback: remove the tag
            await refresh_player_name(character, session, has_custom_parts=False)
            return

        parts = main_vehicle.get("parts", [])
        # Whitelist police parts for officers on active duty
        whitelist = None
        is_on_duty = await PoliceSession.objects.filter(
            character=character, ended_at__isnull=True
        ).aexists()
        if is_on_duty:
            whitelist = POLICE_DUTY_WHITELIST
        custom_parts = detect_custom_parts(parts, whitelist=whitelist)
        incompatible_parts = detect_incompatible_parts(
            parts, main_vehicle["fullName"]
        )

        await refresh_player_name(
            character, session,
            has_custom_parts=bool(custom_parts or incompatible_parts),
        )


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
        thread_sensitive=True,  # Important for database connections!
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
        thread_sensitive=True,  # Important for database connections!
    )
    await async_execute_raw_sql(raw_sql, params)


async def process_log_event(
    event: LogEvent, http_client=None, http_client_mod=None, ctx={}, hostname=""
):
    discord_client = ctx.get("discord_client")
    timestamp = event.timestamp
    is_current_event = ctx.get("startup_time") and timestamp > ctx.get("startup_time")

    forward_message = None

    match event:
        case PlayerChatMessageLogEvent(timestamp, player_name, player_id, message):
            (
                character,
                player,
                character_created,
                player_info,
            ) = await aget_or_create_character(player_name, player_id, http_client_mod)
            await PlayerChatLog.objects.acreate(
                timestamp=timestamp,
                character=character,
                text=message,
            )

            # --- New Command Framework ---
            from amc.command_framework import registry, CommandContext

            cmd_ctx = CommandContext(
                timestamp=timestamp,
                character=character,
                player=player,
                http_client=http_client,
                http_client_mod=http_client_mod,
                discord_client=discord_client,
                player_info=player_info or {},  # Ensure dict
                is_current_event=bool(is_current_event),
            )

            # Fire-and-forget: don't block event processing on command execution
            asyncio.create_task(registry.execute(message, cmd_ctx))

            # Emit SSE event for all chat messages (allows bot to build conversation history)
            # Fire-and-forget to avoid blocking event processing
            if is_current_event:
                from amc.api.bot_events import emit_bot_event

                is_bot_command = message.startswith("/bot ")
                asyncio.create_task(
                    emit_bot_event(
                        {
                            "type": "chat_message",
                            "timestamp": timestamp.isoformat(),
                            "player_name": player_name,
                            "player_id": str(player_id),
                            "discord_id": player.discord_user_id if player else None,
                            "character_guid": str(character.guid)
                            if character and character.guid
                            else None,
                            "message": message[5:] if is_bot_command else message,
                            "is_bot_command": is_bot_command,
                        }
                    )
                )

            if (
                discord_client
                and ctx.get("startup_time")
                and timestamp > ctx.get("startup_time")
            ):
                forward_message = (
                    settings.DISCORD_GAME_CHAT_CHANNEL_ID,
                    f"**{player_name}:** {message}",
                )

        case AnnouncementLogEvent(timestamp, message):
            if (
                discord_client
                and ctx.get("startup_time")
                and timestamp > ctx.get("startup_time")
            ):
                forward_message = (
                    settings.DISCORD_GAME_CHAT_CHANNEL_ID,
                    f"📢 {message}",
                )

        case PlayerVehicleLogEvent(
            timestamp, player_name, player_id, vehicle_name, vehicle_id
        ):
            action = PlayerVehicleLog.action_for_event(event)
            character, player, *_ = await aget_or_create_character(
                player_name, player_id, http_client_mod
            )
            await PlayerVehicleLog.objects.acreate(
                timestamp=timestamp,
                character=character,
                vehicle_game_id=vehicle_id,
                vehicle_name=vehicle_name,
                action=action,
            )
            if action == PlayerVehicleLog.Action.ENTERED:
                if "Police" in vehicle_name:
                    asyncio.create_task(
                        _show_police_popup(
                            http_client_mod,
                            character_guid=character.guid,
                            player_id=str(player.unique_id),
                        )
                    )

            if action in [
                PlayerVehicleLog.Action.ENTERED,
                PlayerVehicleLog.Action.EXITED,
            ]:
                asyncio.create_task(
                    handle_player_vehicle_mod_check(
                        character, player, http_client_mod, action
                    )
                )

            #  asyncio.create_task(delay(register_player_vehicles(http_client_mod, character, player), 5))
            if action == PlayerVehicleLog.Action.BOUGHT and vehicle_name == "Vulcan":
                await player_donation(2_250_000, character)
            if action == PlayerVehicleLog.Action.SOLD and is_current_event:
                asyncio.create_task(
                    on_vehicle_sold(character, vehicle_name, http_client_mod)
                )
            if (
                discord_client
                and ctx.get("startup_time")
                and timestamp > ctx.get("startup_time")
            ):
                forward_message = (
                    settings.DISCORD_VEHICLE_LOGS_CHANNEL_ID,
                    f"{player_name} ({player_id}) {action.label} vehicle: {vehicle_name} ({vehicle_id})",
                )

        case PlayerLoginLogEvent(timestamp, player_name, player_id):
            (
                character,
                player,
                character_created,
                player_info,
            ) = await aget_or_create_character(player_name, player_id, http_client_mod)
            is_current_event = ctx.get("startup_time") and timestamp > ctx.get(
                "startup_time"
            )

            # --- Immediate actions (no GUID needed) ---
            if character:
                await process_login_event(character.id, timestamp)
                asyncio.create_task(send_player_messages(http_client_mod, player))

            if is_current_event:
                # Welcome announcement in global chat (doesn't need GUID)
                try:
                    welcome_message, _is_new = get_welcome_message(
                        character.name,
                        is_new=character_created,
                        last_online=character.last_online,
                    )
                    if welcome_message:
                        asyncio.create_task(
                            announce(welcome_message, http_client, delay=5)
                        )
                except Exception as e:
                    logger.exception(f"Failed to greet player: {e}")

                # Fire-and-forget: GUID-dependent actions (popup, tag checks, teleport)
                asyncio.create_task(
                    _login_guid_dependent_actions(
                        character,
                        player,
                        player_name,
                        player_id,
                        http_client,
                        http_client_mod,
                        character_created,
                    )
                )

                # Fire-and-forget: sync faction Discord role on login
                if discord_client and player.discord_user_id:
                    try:
                        membership = await FactionMembership.objects.aget(player=player)
                        guild = discord_client.get_guild(settings.DISCORD_GUILD_ID)
                        if guild:
                            member = guild.get_member(player.discord_user_id)
                            if member:
                                from amc_cogs.faction import sync_faction_discord_role
                                discord_client.loop.create_task(
                                    sync_faction_discord_role(
                                        guild, member, membership.faction
                                    )
                                )
                    except FactionMembership.DoesNotExist:
                        pass

            if (
                discord_client
                and ctx.get("startup_time")
                and timestamp > ctx.get("startup_time")
            ):
                forward_message = (
                    settings.DISCORD_GAME_CHAT_CHANNEL_ID,
                    f"**🟢 Player Login:** {player_name}",
                )

        case PlayerLogoutLogEvent(timestamp, player_name, player_id):
            character = (
                await Character.objects.with_last_login()
                .filter(
                    name=player_name, guid__isnull=False, player__unique_id=player_id
                )
                .order_by("-last_login")
                .afirst()
            )
            if character:
                await process_logout_event(character.id, timestamp)
            if (
                discord_client
                and ctx.get("startup_time")
                and timestamp > ctx.get("startup_time")
            ):
                forward_message = (
                    settings.DISCORD_GAME_CHAT_CHANNEL_ID,
                    f"**🔴 Player Logout:** {player_name}",
                )

        case LegacyPlayerLogoutLogEvent(timestamp, player_name):
            character = await Character.objects.aget(
                Exists(
                    PlayerStatusLog.objects.filter(
                        character=OuterRef("pk"), timespan__upper_inf=True
                    )
                ),
                name=player_name,
            )
            await process_logout_event(character.id, timestamp)

        case CompanyAddedLogEvent(
            timestamp, company_name, is_corp, owner_name, owner_id
        ) | CompanyRemovedLogEvent(
            timestamp, company_name, is_corp, owner_name, owner_id
        ):
            character, *_ = await aget_or_create_character(
                owner_name, owner_id, http_client_mod
            )
            company, company_created = await Company.objects.aget_or_create(
                name=company_name,
                owner=character,
                is_corp=is_corp,
                defaults={"first_seen_at": timestamp},
            )
            if company_created and is_corp:
                # Announce license requirements
                pass

        case PlayerRestockedDepotLogEvent(timestamp, player_name, depot_name):
            # TODO: skip if no client
            player_id = None
            if http_client:
                players = await get_players(http_client)
                for p_id, p_data in players:
                    if player_name == p_data["name"]:
                        player_id = p_id
                        break
            if player_id is None:
                raise Exception("Player not found")

            character = (
                await Character.objects.select_related("player")
                .filter(name=player_name, player__unique_id=int(player_id))
                .alatest("status_logs__timespan__startswith")
            )
            await PlayerRestockDepotLog.objects.acreate(
                timestamp=timestamp,
                character=character,
                depot_name=depot_name,
            )
            if (
                discord_client
                and ctx.get("startup_time")
                and timestamp > ctx.get("startup_time")
            ):
                forward_message = (
                    settings.DISCORD_GAME_CHAT_CHANNEL_ID,
                    f"**📦 Player Restocked Depot:** {player_name} (Depot: {depot_name})",
                )
                subsidy_amount = 10_000
                asyncio.create_task(
                    on_player_profit(
                        character, subsidy_amount, 0, http_client_mod
                    )
                )

        case PlayerCreatedCompanyLogEvent(timestamp, player_name, company_name):
            # Handled by CompanyAddedLogEvent, if created
            pass

        case PlayerLevelChangedLogEvent(
            timestamp, player_name, player_id, level_type, level_value
        ):
            match level_type:
                case "CL_Driver":
                    field_name = "driver_level"
                case "CL_Bus":
                    field_name = "bus_level"
                case "CL_Taxi":
                    field_name = "taxi_level"
                case "CL_Police":
                    field_name = "police_level"
                case "CL_Truck":
                    field_name = "truck_level"
                case "CL_Wrecker":
                    field_name = "wrecker_level"
                case "CL_Racer":
                    field_name = "racer_level"
                case _:
                    raise ValueError("Unknown level type")
            await Character.objects.filter(
                name=player_name, player__unique_id=player_id
            ).aupdate(**{field_name: level_value})

        case ServerStartedLogEvent(timestamp, _version):

            async def spawn_dealerships():
                async for vd in VehicleDealership.objects.filter(spawn_on_restart=True):
                    await vd.spawn(http_client_mod)

            async def spawn_player_vehicles():
                async for v in CharacterVehicle.objects.select_related(
                    "character"
                ).filter(spawn_on_restart=True):
                    extra_data = {}
                    if v.character:
                        extra_data = {
                            "companyGuid": "1" * 32,
                            "companyName": f"{v.character.name}'s Display",
                            "drivable": v.rental,
                        }
                    tags = [f"display-{v.id}"]
                    if v.character:
                        tags.append(v.character.name)
                    await spawn_registered_vehicle(
                        http_client_mod,
                        v,
                        tag="display_vehicles",
                        extra_data=extra_data,
                        tags=tags,
                    )

            async def spawn_world_vehicles():
                async for v in CharacterVehicle.objects.filter(pk=2367):
                    await set_world_vehicle_decal(
                        http_client_mod,
                        f"{v.config['VehicleName']}_C",
                        customization=v.config["Customization"],
                        decal=v.config["Decal"],
                        parts=[{**p, "partKey": p["Key"]} for p in v.config["Parts"]],
                    )

            async def spawn_garages():
                async for g in Garage.objects.filter(spawn_on_restart=True):
                    if not g.config:
                        continue
                    location = g.config.get("Location")
                    rotation = g.config.get("Rotation")
                    if not location:
                        continue

                    resp = await spawn_garage(http_client_mod, location, rotation)
                    tag = resp.get("tag")
                    g.tag = tag
                    await g.asave(update_fields=["tag"])

            async def _spawn_assets():
                async for wt in WorldText.objects.filter():
                    await spawn_assets(http_client_mod, wt.generate_asset_data())
                async for wt in WorldObject.objects.filter():
                    await spawn_assets(http_client_mod, [wt.generate_asset_data()])

            asyncio.create_task(delay(spawn_dealerships(), 15))
            asyncio.create_task(delay(_spawn_assets(), 20))
            asyncio.create_task(delay(spawn_garages(), 25))

        case UnknownLogEntry():
            logger.warning("Unknown log entry: %s", event)
        case SecurityAlertLogEvent():
            pass
        case _:
            pass

    if (
        forward_message
        and discord_client
        and ctx.get("startup_time")
        and timestamp > ctx.get("startup_time")
        and hostname == "asean-mt-server"
    ):
        channel_id, content = forward_message
        enqueue_discord_message(channel_id, content, timestamp)


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
        return {"status": "duplicate", "timestamp": event.timestamp}

    # TODO rename context variable names
    # Separate main server and event server sessions
    match log.hostname:
        case "asean-mt-server":
            http_client = ctx.get("http_client")
            http_client_mod = ctx.get("http_client_mod")
        case "motortown-server-event":
            http_client = ctx.get("http_client_event")
            http_client_mod = ctx.get("http_client_event_mod")
        case _:
            http_client = ctx.get("http_client")
            http_client_mod = ctx.get("http_client_mod")

    await process_log_event(
        event,
        http_client=http_client,
        http_client_mod=http_client_mod,
        ctx=ctx,
        hostname=log.hostname,
    )

    server_log.event_processed = True
    await server_log.asave(update_fields=["event_processed"])

    return {"status": "created", "timestamp": event.timestamp}
