"""Teleport and vehicle reset event handlers.

Handles: ServerResetVehicleAt, ServerTeleportCharacter,
ServerTeleportVehicle, ServerRespawnCharacter

Note: on-duty police do NOT get teleport redirects here. Police duty now
carries the R tag (player_tags.build_display_name), so the server mod's
RP hooks block every teleport an on-duty officer attempts — the same
enforcement wanted players already get. There is nothing left to redirect.
"""

from __future__ import annotations

import logging

from amc.handlers import register
from amc.models import ServerTeleportLog
from amc.mod_server import show_popup

logger = logging.getLogger("amc.webhook.handlers.teleport")


# ---------------------------------------------------------------------------
# ServerResetVehicleAt
# ---------------------------------------------------------------------------

# A vehicle reset landing within this many units (1 unit ~ 1 cm in the game's
# coordinate space; 200 units = 2 m) of a delivery point flags the player:
# popup + Discord alert, and every cargo arrival from them for the next
# CARGO_IGNORE_SECONDS is ignored by the cargo handler.
RESET_NEAR_DP_UNITS = 200
# Also flag when roadside recovery teleported the vehicle >1 km to reach the
# character: their last driving position is >1 km from where they stand now.
RESET_FAR_RECOVERY_UNITS = 100_000  # 1 km
# Only trust driving-location rows at most this old when measuring the
# recovery teleport distance.
VEHICLE_ROW_WINDOW_SECONDS = 600
CARGO_IGNORE_SECONDS = 300  # 5 minutes
# last_location/last_online are refreshed by the positions monitor; only
# trust them if they are at most this old when the reset arrives.
LOCATION_FRESHNESS_SECONDS = 30

# Opaque by design: tells the player they were flagged and to contact an
# admin — no mention of the cargo-ignore window or its duration.
FLAG_POPUP_TEXT = (
    "You have been flagged. If you believe this is a mistake, "
    "please contact an admin."
)


async def _nearest_delivery_point(point):
    """Return (delivery_point, distance_units) or (None, None)."""
    import math

    from amc.models import DeliveryPoint

    best, best_dist = None, None
    async for dp in DeliveryPoint.objects.filter(removed=False).only(
        "guid", "name", "coord"
    ):
        d = math.hypot(point.x - dp.coord.x, point.y - dp.coord.y)
        if best_dist is None or d < best_dist:
            best, best_dist = dp, d
    return best, best_dist


async def _reset_flag_reason(character, now):
    """Return (reason_text, delivery_point_name, distance_units) or all-None.

    Trigger 1: the reset lands within RESET_NEAR_DP_UNITS of a delivery
    point.  Trigger 2: the character's last driving position (the vehicle
    roadside recovery would teleport) is more than RESET_FAR_RECOVERY_UNITS
    from where they stand now.
    """
    import math

    from amc.models import CharacterLocation

    dp, dp_dist = await _nearest_delivery_point(character.last_location)
    if dp is not None and dp_dist is not None and dp_dist <= RESET_NEAR_DP_UNITS:
        return (
            f"Reset landed {dp_dist / 100:.1f} m from delivery point {dp.name}",
            dp.name,
            dp_dist,
        )

    # Far-recovery: last row where the character was driving, within the
    # freshness window; the vehicle was there before recovery.
    last_drive = (
        await CharacterLocation.objects.filter(
            character=character,
            vehicle_key__isnull=False,
            timestamp__gte=now
            - __import__("datetime").timedelta(seconds=VEHICLE_ROW_WINDOW_SECONDS),
        )
        .order_by("-timestamp")
        .afirst()
    )
    if last_drive is not None:
        recover_dist = math.hypot(
            last_drive.location.x - character.last_location.x,
            last_drive.location.y - character.last_location.y,
        )
        if recover_dist > RESET_FAR_RECOVERY_UNITS:
            return (
                "Roadside recovery teleported the vehicle "
                f"{recover_dist / 100:.0f} m to the character",
                None,
                recover_dist,
            )
    return None, None, None


@register("ServerResetVehicleAt")
async def handle_reset_vehicle(event, player, character, ctx):
    from datetime import timedelta

    from django.utils import timezone

    from amc.pipeline.discord import post_discord_reset_flag_alert

    if ctx.is_rp_mode and ctx.http_client_mod:
        await show_popup(
            ctx.http_client_mod,
            "Teleporting is disabled in RP mode.",
            character_guid=character.guid,
        )
        return 0, 0, 0, 0

    # Reset flag — two triggers, same consequence:
    #   1. Reset lands within RESET_NEAR_DP_UNITS of a delivery point
    #      (teleport the loaded vehicle right onto the DP, unload instantly).
    #   2. Roadside recovery teleported the vehicle >1 km to reach the
    #      character (their last driving position is >1 km from where they
    #      stand now).  Recovery itself is a legit feature — the flag only
    #      pauses cargo arrivals so teleported cargo can't be dumped.
    # Both need a fresh last_location (positions monitor).
    if character is not None:
        now = timezone.now()
        location_fresh = (
            character.last_location is not None
            and character.last_online is not None
        )
        open("/tmp/rf2.log","a").write(f"fresh={location_fresh} age={(now-character.last_online).total_seconds() if character.last_online else None}\n")
        if location_fresh and now - character.last_online <= timedelta(
            seconds=LOCATION_FRESHNESS_SECONDS
        ):
            reason, dp_name, dist = await _reset_flag_reason(character, now)
            if reason:
                character.cargo_ignore_until = now + timedelta(
                    seconds=CARGO_IGNORE_SECONDS
                )
                await character.asave(update_fields=["cargo_ignore_until"])
                if ctx.http_client_mod:
                    await show_popup(
                        ctx.http_client_mod,
                        FLAG_POPUP_TEXT,
                        character_guid=character.guid,
                    )
                post_discord_reset_flag_alert(
                    ctx.discord_client,
                    character_name=character.name,
                    player_id=str(character.player.unique_id),
                    detail=reason,
                    delivery_point_name=dp_name,
                    distance_units=dist,
                )
                logger.warning(
                    "Vehicle reset flagged: player=%s reason=%s dp=%s "
                    "dist=%.0f units — cargo ignored for %ds",
                    character.player.unique_id,
                    reason,
                    dp_name,
                    dist,
                    CARGO_IGNORE_SECONDS,
                )
    return 0, 0, 0, 0


# ---------------------------------------------------------------------------
# ServerTeleportCharacter / ServerTeleportVehicle / ServerRespawnCharacter
# ---------------------------------------------------------------------------


@register("ServerTeleportCharacter")
async def _handle_teleport_character(event, player, character, ctx):
    return await _handle_teleport_or_respawn(event, character, ctx)


@register("ServerTeleportVehicle")
async def _handle_teleport_vehicle(event, player, character, ctx):
    return await _handle_teleport_or_respawn(event, character, ctx)


@register("ServerRespawnCharacter")
async def _handle_respawn_character(event, player, character, ctx):
    return await _handle_teleport_or_respawn(event, character, ctx)


async def _handle_teleport_or_respawn(event, character, ctx):
    """Log teleport/respawn events and show RP mode popup if applicable.

    Fires on ServerTeleportCharacter / ServerTeleportVehicle /
    ServerRespawnCharacter.  All teleports are logged to ServerTeleportLog
    for audit purposes.  No auto-arrest or other side-effects.
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

    if ctx.is_rp_mode and ctx.http_client_mod:
        await show_popup(
            ctx.http_client_mod,
            "Teleporting is disabled in RP mode.",
            character_guid=character.guid,
        )

    return 0, 0, 0, 0


def _parse_timestamp(event):
    from amc.handlers.utils import parse_event_timestamp

    return parse_event_timestamp(event)
