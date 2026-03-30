import asyncio
import math
import re
from django.core.cache import cache
from amc.command_framework import registry, CommandContext
from amc.game_server import get_players
from amc.models import ArrestZone, Character, PoliceSession, TeleportPoint, Delivery, Confiscation
from amc.mod_server import force_exit_vehicle, send_system_message, show_popup, teleport_player, transfer_money
from amc_finance.services import record_treasury_confiscation_income, send_fund_to_player_wallet
from amc.police import record_confiscation_for_level
from django.utils import timezone
from datetime import timedelta
from django.utils.translation import gettext as gettext, gettext_lazy

# 100 game units = 1 metre
ARREST_RADIUS_ON_FOOT = 5000  # 50m — cop on foot must be within 50m of suspect
ARREST_RADIUS_IN_VEHICLE = 3375  # 33.75m — cop in vehicle (50% more than original 22.5m)
SUSPECT_SPEED_LIMIT = 1500  # 15m per poll tick — suspects moving faster are removed
ARREST_POLL_COUNT = 3  # 3 polls × 1s = 3 seconds
ARREST_COOLDOWN = 0  # seconds between arrests per cop
ARREST_CONFISCATION_WINDOW = 10  # minutes — deliveries older than this are safe

_LOC_RE = re.compile(
    r"X=(?P<x>[-\d.]+)\s+Y=(?P<y>[-\d.]+)\s+Z=(?P<z>[-\d.]+)"
)


def parse_location_string(loc_str: str) -> tuple[float, float, float]:
    """Parse 'X=-53918.590 Y=153629.920 Z=-20901.710' → (x, y, z)."""
    m = _LOC_RE.search(loc_str)
    if not m:
        raise ValueError(f"Cannot parse location: {loc_str}")
    return float(m["x"]), float(m["y"]), float(m["z"])


def _distance_3d(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _build_player_locations(players: list) -> dict[str, tuple[str, tuple[float, float, float], bool]]:
    """Build guid → (unique_id, (x,y,z), has_vehicle) mapping from game server player list."""
    result = {}
    for _uid, pdata in players:
        guid = pdata.get("character_guid")
        loc_str = pdata.get("location")
        if not guid or not loc_str:
            continue
        try:
            loc = parse_location_string(loc_str)
        except ValueError:
            continue
        has_vehicle = "vehicle" in pdata
        result[guid] = (pdata["unique_id"], loc, has_vehicle)
    return result


async def execute_arrest(
    officer_character,
    targets: dict,
    target_chars: dict,
    http_client,
    http_client_mod,
) -> tuple[list[str], int]:
    """Execute arrest: teleport to jail, confiscate money, announce.

    Args:
        officer_character: The arresting officer's Character model.
        targets: guid -> (unique_id, location, has_vehicle) for each suspect.
        target_chars: guid -> Character model for each suspect.
        http_client: Game server HTTP client (for announcements).
        http_client_mod: Mod server HTTP client (for teleport, money, messages).

    Returns:
        (arrested_names, total_confiscated) tuple.
    """
    try:
        jail_tp = await TeleportPoint.objects.aget(name__iexact="jail")
        jail_location = {"X": jail_tp.location.x, "Y": jail_tp.location.y, "Z": jail_tp.location.z}
    except TeleportPoint.DoesNotExist:
        raise ValueError("Jail teleport point not configured.")

    arrested_names = []
    total_confiscated = 0
    for guid, (crim_uid, crim_loc, has_vehicle) in targets.items():
        # Exit vehicle
        if has_vehicle:
            try:
                await force_exit_vehicle(http_client_mod, guid)
                await asyncio.sleep(1.5)
            except Exception:
                pass

        # Teleport to jail
        await teleport_player(
            http_client_mod,
            crim_uid,
            jail_location,
            no_vehicles=True,
        )

        # Popup notification
        await show_popup(
            http_client_mod,
            "You have been arrested!",
            player_id=crim_uid,
        )

        name = target_chars[guid].name if guid in target_chars else "Unknown"
        arrested_names.append(name)

        # Money confiscation logic — linear scaling by evasion time
        suspect_char = target_chars.get(guid)
        if suspect_char:
            now = timezone.now()
            window_start = now - timedelta(minutes=ARREST_CONFISCATION_WINDOW)
            recent_deliveries = [
                d async for d in Delivery.objects.filter(
                    character=suspect_char,
                    cargo_key="Money",
                    timestamp__gte=window_start,
                )
            ]

            confiscated_amount = 0
            for delivery in recent_deliveries:
                elapsed_minutes = (now - delivery.timestamp).total_seconds() / 60
                rate = max(0.0, 1.0 - elapsed_minutes / ARREST_CONFISCATION_WINDOW)
                confiscated_amount += round(delivery.payment * rate)

            if confiscated_amount > 0:
                await Confiscation.objects.acreate(
                    character=suspect_char,
                    officer=officer_character,
                    cargo_key="Money",
                    amount=confiscated_amount,
                )

                await suspect_char.arefresh_from_db(fields=["criminal_laundered_total"])
                new_criminal_total = max(0, suspect_char.criminal_laundered_total - confiscated_amount)
                suspect_char.criminal_laundered_total = new_criminal_total
                await suspect_char.asave(update_fields=["criminal_laundered_total"])

                await transfer_money(
                    http_client_mod,
                    int(-confiscated_amount),
                    "Money Confiscated",
                    str(suspect_char.player_id),
                )

                await record_treasury_confiscation_income(confiscated_amount, "Police Confiscation")

                await record_confiscation_for_level(
                    officer_character, confiscated_amount, http_client=http_client, session=http_client_mod
                )

                # Reward officer with confiscated amount
                await transfer_money(
                    http_client_mod,
                    int(confiscated_amount),
                    "Confiscation Reward",
                    str(officer_character.player_id),
                )
                await send_fund_to_player_wallet(confiscated_amount, officer_character, "Confiscation Reward")

                total_confiscated += confiscated_amount
                await send_system_message(
                    http_client_mod,
                    gettext("Confiscated ${amount:,} in illegal earnings from {name}. You earned ${amount:,} confiscation reward.").format(
                        amount=confiscated_amount, name=name
                    ),
                    character_guid=officer_character.guid
                )
                await send_system_message(
                    http_client_mod,
                    gettext("Police confiscated ${amount:,} in illegal earnings from your account.").format(
                        amount=confiscated_amount
                    ),
                    character_guid=suspect_char.guid
                )

    return arrested_names, total_confiscated


@registry.register(
    ["/arrest", "/a"],
    description=gettext_lazy("Arrest nearby suspects (Cops only)"),
    category="Faction",
    featured=True,
)
async def cmd_arrest(ctx: CommandContext):
    # 1. Verify active police session
    is_cop = await PoliceSession.objects.filter(
        character=ctx.character, ended_at__isnull=True
    ).aexists()
    if not is_cop:
        await send_system_message(ctx.http_client_mod, gettext("You must be on police duty to use this command. Use /police to start."), character_guid=ctx.character.guid)
        return

    # 2. Cooldown check
    cooldown_key = f"arrest_cooldown:{ctx.player.unique_id}"
    if cache.get(cooldown_key):
        await send_system_message(ctx.http_client_mod, gettext("You must wait before making another arrest."), character_guid=ctx.character.guid)
        return

    # 3. Initial poll — get all player positions
    players = await get_players(ctx.http_client)
    if not players:
        await send_system_message(ctx.http_client_mod, gettext("Could not fetch player data."), character_guid=ctx.character.guid)
        return

    locations = _build_player_locations(players)

    # Find cop's own position
    cop_guid = ctx.character.guid
    if cop_guid not in locations:
        await send_system_message(ctx.http_client_mod, gettext("Could not determine your position."), character_guid=ctx.character.guid)
        return

    cop_uid, cop_loc, cop_has_vehicle = locations[cop_guid]

    # 3b. Zone check — cop must be inside an active ArrestZone
    from django.contrib.gis.geos import Point
    cop_point = Point(cop_loc[0], cop_loc[1], srid=3857)
    zones_exist = await ArrestZone.objects.filter(active=True).aexists()
    if zones_exist:
        in_zone = await ArrestZone.objects.filter(
            active=True, polygon__contains=cop_point
        ).aexists()
        if not in_zone:
            await send_system_message(ctx.http_client_mod, gettext("Arrests can only be made in designated arrest zones."), character_guid=ctx.character.guid)
            return

    # 4. Find nearby non-police players
    other_guids = [g for g in locations if g != cop_guid]
    if not other_guids:
        await send_system_message(ctx.http_client_mod, gettext("No players nearby."), character_guid=ctx.character.guid)
        return

    # Batch query: exclude characters who have active police sessions
    cop_guids = set()
    async for char in (
        Character.objects.filter(guid__in=other_guids)
    ):
        has_session = await PoliceSession.objects.filter(
            character=char, ended_at__isnull=True
        ).aexists()
        if has_session:
            cop_guids.add(char.guid)

    suspect_guids = [g for g in other_guids if g not in cop_guids]

    if not suspect_guids:
        await send_system_message(ctx.http_client_mod, gettext("No suspects nearby."), character_guid=ctx.character.guid)
        return

    # Filter to suspects within arrest radius (depends on whether cop is on foot or in vehicle)
    arrest_radius = ARREST_RADIUS_IN_VEHICLE if cop_has_vehicle else ARREST_RADIUS_ON_FOOT
    targets = {}  # guid → (unique_id, initial_loc, has_vehicle)
    for guid in suspect_guids:
        if guid not in locations:
            continue
        dist = _distance_3d(cop_loc, locations[guid][1])
        if dist <= arrest_radius:
            targets[guid] = locations[guid]

    if not targets:
        await send_system_message(ctx.http_client_mod, gettext("No suspects within arrest range."), character_guid=ctx.character.guid)
        return

    # Look up names for all targets
    target_chars = {}
    async for char in Character.objects.filter(guid__in=targets.keys()):
        target_chars[char.guid] = char

    target_names = [target_chars[g].name for g in targets if g in target_chars]
    names_str = ", ".join(target_names)

    # 5. Notify cop
    await send_system_message(
        ctx.http_client_mod,
        gettext("Arresting {names}… stay close for 3 seconds.").format(names=names_str),
        character_guid=ctx.character.guid,
    )

    # Track previous suspect positions for speed check
    prev_suspect_locs = {guid: targets[guid][1] for guid in targets}

    # 6. Poll loop — check every second for 3 seconds
    for i in range(ARREST_POLL_COUNT):
        await asyncio.sleep(1)

        players = await get_players(ctx.http_client)
        if not players:
            await send_system_message(ctx.http_client_mod, gettext("Lost connection to server. Arrest cancelled."), character_guid=ctx.character.guid)
            return

        current_locations = _build_player_locations(players)

        # Check cop still online
        if cop_guid not in current_locations:
            return  # cop disconnected, silently abort

        cop_uid_now, current_cop_loc, cop_veh = current_locations[cop_guid]

        # Check each target criminal
        for guid in list(targets.keys()):
            if guid not in current_locations:
                # Criminal went offline — remove from targets
                name = target_chars[guid].name if guid in target_chars else "Unknown"
                await send_system_message(
                    ctx.http_client_mod,
                    gettext("{name} went offline. Removed from arrest.").format(name=name),
                    character_guid=ctx.character.guid,
                )
                del targets[guid]
                prev_suspect_locs.pop(guid, None)
                continue

            crim_uid, current_criminal_loc, crim_veh = current_locations[guid]

            # Speed check: only applies to suspects in vehicles
            if crim_veh:
                prev_loc = prev_suspect_locs[guid]
                suspect_speed = _distance_3d(prev_loc, current_criminal_loc)
                if suspect_speed > SUSPECT_SPEED_LIMIT:
                    name = target_chars[guid].name if guid in target_chars else "Unknown"
                    await send_system_message(
                        ctx.http_client_mod,
                        gettext("{name} is moving too fast. Removed from arrest.").format(name=name),
                        character_guid=ctx.character.guid,
                    )
                    del targets[guid]
                    prev_suspect_locs.pop(guid, None)
                    continue

            # Proximity check: cop must stay within radius of suspect
            current_radius = ARREST_RADIUS_IN_VEHICLE if cop_veh else ARREST_RADIUS_ON_FOOT
            if _distance_3d(current_cop_loc, current_criminal_loc) > current_radius:
                name = target_chars[guid].name if guid in target_chars else "Unknown"
                await send_system_message(
                    ctx.http_client_mod,
                    gettext("{name} is no longer within range. Removed from arrest.").format(
                        name=name
                    ),
                    character_guid=ctx.character.guid,
                )
                del targets[guid]
                prev_suspect_locs.pop(guid, None)
                continue

            # Update for next tick
            prev_suspect_locs[guid] = current_criminal_loc
            targets[guid] = (crim_uid, current_criminal_loc, crim_veh)

        if not targets:
            await send_system_message(ctx.http_client_mod, gettext("All targets escaped. Arrest cancelled."), character_guid=ctx.character.guid)
            return

    # 7. Execute arrests
    try:
        arrested_names, total_confiscated = await execute_arrest(
            officer_character=ctx.character,
            targets=targets,
            target_chars=target_chars,
            http_client=ctx.http_client,
            http_client_mod=ctx.http_client_mod,
        )
    except ValueError as e:
        await send_system_message(ctx.http_client_mod, gettext(str(e)), character_guid=ctx.character.guid)
        return

    if not arrested_names:
        await send_system_message(ctx.http_client_mod, gettext("All targets escaped. Arrest cancelled."), character_guid=ctx.character.guid)
        return

    # Set cooldown
    cache.set(cooldown_key, True, timeout=ARREST_COOLDOWN)

    # Announce and confirm
    names_arrested = ", ".join(arrested_names)
    if total_confiscated > 0:
        await ctx.announce(
            f"{names_arrested} arrested by {ctx.character.name}! ${total_confiscated:,} confiscated."
        )
    else:
        await ctx.announce(
            f"{names_arrested} arrested by {ctx.character.name}!"
        )
    await send_system_message(
        ctx.http_client_mod,
        gettext("{names} arrested and sent to jail.").format(names=names_arrested),
        character_guid=ctx.character.guid,
    )
