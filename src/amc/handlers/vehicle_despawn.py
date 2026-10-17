"""Vehicle despawn audit handler.

Handles: ServerDespawnVehicle

Every ServerDespawnVehicle RPC is logged for moderation: player /d, mod
despawn endpoints (they route through ``PC:ServerDespawnVehicle``), and any
direct RPC invocation from a modified client. The C++ hook payload carries the
calling controller's CharacterGuid (base event field), the Vehicle object
(Name / Net_VehicleId / Class), and the vehicle's registered owner
(OwnerCharacterGuid / OwnerName) — a caller that differs from the owner is the
grief/cheat signal.
"""

from __future__ import annotations

import logging

from amc.handlers import register
from amc.handlers.utils import parse_event_timestamp
from amc.models import ServerVehicleDespawnLog

logger = logging.getLogger("amc.webhook.handlers.vehicle_despawn")


@register("ServerDespawnVehicle")
async def handle_despawn_vehicle(event, player, character, ctx):
    timestamp = parse_event_timestamp(event)
    data = event.get("data") if isinstance(event, dict) else None
    vehicle = (data or {}).get("Vehicle") or {}
    await ServerVehicleDespawnLog.objects.acreate(
        timestamp=timestamp,
        player=character.player if character else None,
        character=character,
        hook="ServerDespawnVehicle",
        vehicle_game_id=vehicle.get("Net_VehicleId"),
        vehicle_name=vehicle.get("Name"),
        data=data,
    )
    return 0, 0, 0, 0
