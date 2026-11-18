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

# Flag when roadside recovery teleported the vehicle >1 km to reach the
# character: their last driving position is >1 km from where they stand now.
# (freeman 2026-09-26: the earlier near-delivery-point trigger was REMOVED —
# legit long-distance tow jobs land wrecks right at delivery points and
# false-flagged towers; only the far-recovery signal remains.)
RESET_FAR_RECOVERY_UNITS = 100_000  # 1 km
# Only trust driving-location rows at most this old when measuring the
# recovery teleport distance.
VEHICLE_ROW_WINDOW_SECONDS = 600
CARGO_IGNORE_SECONDS = 300  # 5 minutes
# last_location/last_online are refreshed by the positions monitor; only
# trust them if they are at most this old when the reset arrives.
LOCATION_FRESHNESS_SECONDS = 30
# A roadside recovery happens while the player is ONLINE: the positions
# monitor samples them every couple of seconds, so the driving row and the
# current position are joined by an unbroken run of rows. A LOGIN breaks that
# run — the game re-places the character (nearest-road / respawn correction)
# and the pre-logout driving row sits kilometres away even though nobody
# teleported. Any sampling gap this large between the driving row and the
# reset means the two positions came from different sessions, so the distance
# between them measures the login, not a recovery (freeman 2026-11-18).
RECOVERY_SESSION_GAP_SECONDS = 120


# Opaque by design: tells the player they were flagged and to contact an
# admin — no mention of the cargo-ignore window or its duration.
FLAG_POPUP_TEXT = (
    "You have been flagged. If you believe this is a mistake, "
    "please contact an admin."
)


async def _largest_sampling_gap(character, since, now):
    """Largest gap between consecutive sampled positions from ``since`` to ``now``.

    The positions monitor samples online characters every couple of seconds, so
    an unbroken gap means the character never left the server between ``since``
    and ``now`` — i.e. both samples belong to the same session. A login (or a
    game-side re-place) breaks the run instead.

    Short-circuits as soon as the gap exceeds RECOVERY_SESSION_GAP_SECONDS:
    the caller only needs "is there a session break", not its exact size.
    """
    from amc.models import CharacterLocation

    prev = since
    largest = 0.0
    async for ts in (
        CharacterLocation.objects.filter(character=character, timestamp__gte=since)
        .order_by("timestamp")
        .values_list("timestamp", flat=True)
        .aiterator()
    ):
        gap = (ts - prev).total_seconds()
        largest = max(largest, gap)
        if largest > RECOVERY_SESSION_GAP_SECONDS:
            return largest
        prev = ts
    # Tail: the stretch from the last sampled row (or from ``since`` itself
    # when nothing was sampled) up to the reset.
    tail = (now - prev).total_seconds()
    return max(largest, tail)


async def _reset_flag_reason(character, now):
    """Return (reason_text, delivery_point_name, distance_units) or all-None.

    Single trigger: the character's last driving position (the vehicle
    roadside recovery would teleport) is more than RESET_FAR_RECOVERY_UNITS
    from where they stand now. The near-delivery-point trigger was removed
    (freeman 2026-09-26): legit tow jobs deliver wrecks to delivery points.

    The comparison is only meaningful inside ONE session: a real recovery
    happens with the player online, so their samples run continuously from the
    driving row to the reset. When the driving row is separated from now by a
    sampling gap (login / respawn re-place), the distance measures the login,
    not a teleport — skip the flag (freeman 2026-11-18).
    """
    import math

    from amc.models import CharacterLocation

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
            gap = await _largest_sampling_gap(character, last_drive.timestamp, now)
            if gap > RECOVERY_SESSION_GAP_SECONDS:
                logger.info(
                    "Reset flag skipped for %s: %.0fs sampling gap since the "
                    "last driving row %.0fs ago — different session "
                    "(login/reset), not a roadside recovery",
                    character.name,
                    gap,
                    (now - last_drive.timestamp).total_seconds(),
                )
                return None, None, None
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

    # Reset flag — roadside recovery teleported the vehicle >1 km to reach
    # the character (their last driving position is >1 km from where they
    # stand now).  Recovery itself is a legit feature — the flag only
    # pauses cargo arrivals so teleported cargo can't be dumped.
    # (freeman 2026-09-26: the near-delivery-point trigger was removed —
    # legit long-distance tow jobs land wrecks right at DPs and
    # false-flagged towers.)  Needs a fresh last_location (positions
    # monitor).
    if character is not None:
        now = timezone.now()
        location_fresh = (
            character.last_location is not None
            and character.last_online is not None
        )
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
