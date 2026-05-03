"""Teleport and vehicle reset event handlers.

Handles: ServerResetVehicleAt, ServerTeleportCharacter,
ServerTeleportVehicle, ServerRespawnCharacter
"""

from __future__ import annotations

import logging
import math

from amc.handlers import register
from amc.models import (
    PoliceSession,
    ServerTeleportLog,
    Wanted,
)
from amc.game_server import get_players
from amc.mod_server import send_system_message, teleport_player
from amc.police import POLICE_STATIONS

logger = logging.getLogger("amc.webhook.handlers.teleport")


# ---------------------------------------------------------------------------
# ServerResetVehicleAt
# ---------------------------------------------------------------------------


@register("ServerResetVehicleAt")
async def handle_reset_vehicle(event, player, character, ctx):
    return 0, 0, 0, 0


# ---------------------------------------------------------------------------
# ServerTeleportCharacter / ServerTeleportVehicle / ServerRespawnCharacter
# ---------------------------------------------------------------------------


@register("ServerTeleportCharacter")
async def _handle_teleport_character(event, player, character, ctx):
    result = await _handle_teleport_or_respawn(event, character, ctx)
    await _redirect_police_near_wanted(character, player, ctx, "Teleport")
    return result


@register("ServerTeleportVehicle")
async def _handle_teleport_vehicle(event, player, character, ctx):
    return await _handle_teleport_or_respawn(event, character, ctx)


@register("ServerRespawnCharacter")
async def _handle_respawn_character(event, player, character, ctx):
    result = await _handle_teleport_or_respawn(event, character, ctx)
    await _redirect_police_near_wanted(character, player, ctx, "Respawn")
    return result


async def _redirect_police_near_wanted(character, player, ctx, action_label):
    """Redirect on-duty police to the nearest station if they landed near a wanted suspect.

    Used after ServerTeleportCharacter and ServerRespawnCharacter to prevent
    exploiting teleports/respawns to catch suspects.
    """
    is_police = await PoliceSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aexists()
    if not is_police or not ctx.http_client or not ctx.http_client_mod:
        return

    from amc.commands.faction import _build_player_locations
    from amc.commands.police import SETWANTED_MIN_DISTANCE

    players_list = await get_players(ctx.http_client)
    if not players_list:
        return

    locations = _build_player_locations(players_list)

    officer_entry = locations.get(str(character.guid))
    if not officer_entry:
        return
    _officer_name, officer_loc, _officer_vehicle = officer_entry

    wanted_nearby = False
    async for wanted in Wanted.objects.filter(
        expired_at__isnull=True, wanted_remaining__gt=0
    ).select_related("character"):
        guid = wanted.character.guid
        if not guid or guid == str(character.guid):
            continue
        entry = locations.get(guid)
        if not entry:
            continue
        _name, suspect_loc, _vehicle = entry
        if _distance_3d(officer_loc, suspect_loc) < SETWANTED_MIN_DISTANCE:
            wanted_nearby = True
            break

    if not wanted_nearby:
        return

    nearest = None
    min_dist = float("inf")
    for _name, tx, ty, tz in POLICE_STATIONS:
        dist = _distance_3d(officer_loc, (tx, ty, tz))
        if dist < min_dist:
            min_dist = dist
            nearest = (tx, ty, tz)

    if nearest:
        tx, ty, tz = nearest
        await teleport_player(
            ctx.http_client_mod,
            str(player.unique_id),
            {"X": tx, "Y": ty, "Z": tz},
            no_vehicles=True,
        )
        await send_system_message(
            ctx.http_client_mod,
            f"{action_label} redirected — too close to a wanted suspect.",
            character_guid=character.guid,
        )


async def _handle_teleport_or_respawn(event, character, ctx):
    """Send wanted criminals to jail when they teleport via the game.

    Fires on ServerTeleportCharacter / ServerTeleportVehicle /
    ServerRespawnCharacter.

    For wanted players the outcome is:
      - Teleport to jail (+ activate jail boundary via jailed_until).
      - Show a popup explaining why.
      - Wanted record is left active — police must still make a proper arrest.

    For non-wanted players, the event is a no-op (just logged).
    """
    timestamp = _parse_timestamp(event)

    # Log ALL teleports for audit
    hook_name = event.get("hook", "") if isinstance(event, dict) else ""
    await ServerTeleportLog.objects.acreate(
        timestamp=timestamp,
        player=character.player,
        character=character,
        hook=hook_name,
        data=event.get("data"),
    )

    # Only act on actively wanted players
    try:
        wanted = await Wanted.objects.aget(
            character=character, expired_at__isnull=True, wanted_remaining__gt=0
        )
    except Wanted.DoesNotExist:
        return 0, 0, 0, 0

    # Skip police officers
    is_police = await PoliceSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aexists()
    if is_police:
        return 0, 0, 0, 0

    logger.info(
        "Wanted criminal %s triggered %s — sending to jail",
        character.name,
        hook_name,
    )

    from amc.commands.teleport import _auto_arrest_wanted_criminal

    hook_display = hook_name or "teleport"
    await _auto_arrest_wanted_criminal(
        wanted,
        character,
        character.player,
        ctx.http_client_mod,
        reason=f"Arrested for teleporting ({hook_display}) while wanted.",
    )

    return 0, 0, 0, 0


def _distance_3d(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _parse_timestamp(event):
    from amc.handlers.utils import parse_event_timestamp

    return parse_event_timestamp(event)
