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


@register("ServerResetVehicleAt")
async def handle_reset_vehicle(event, player, character, ctx):
    if ctx.is_rp_mode and ctx.http_client_mod:
        await show_popup(
            ctx.http_client_mod,
            "Teleporting is disabled in RP mode.",
            character_guid=character.guid,
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
