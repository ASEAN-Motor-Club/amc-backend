import asyncio
import logging

import aiohttp
from django.conf import settings

from amc.api.player_positions_common import (
    POSITION_UPDATE_SLEEP,
    get_players_mod_masked,
)
from amc.api.player_positions_pb2 import PlayerPositions, VehicleKey

logger = logging.getLogger(__name__)


_VEHICLE_KEY_MAP: dict[str, int] = {
    desc.name.replace("VEHICLE_KEY_", ""): val
    for val, desc in VehicleKey.DESCRIPTOR.values_by_number.items()
    if val != 0
}


def serialize_players(players: list[dict]) -> bytes:
    positions = PlayerPositions()
    for p in players:
        loc = p.get("Location", {})
        pos = positions.players.add()
        pos.unique_id = int(p.get("UniqueID", 0))
        pos.player_name = str(p.get("PlayerName", ""))
        hidden = bool(p.get("hidden", False))
        if hidden:
            pos.x = 0.0
            pos.y = 0.0
            pos.z = 0.0
        else:
            pos.x = float(loc.get("X", 0))
            pos.y = float(loc.get("Y", 0))
            pos.z = float(loc.get("Z", 0))

        raw_key = str(p.get("VehicleKey", ""))
        enum_val = _VEHICLE_KEY_MAP.get(raw_key)
        if not hidden and enum_val is not None:
            pos.vehicle_key_enum = enum_val
        elif not hidden:
            pos.vehicle_key_unknown = raw_key
        pos.hidden = hidden
    return positions.SerializeToString()


async def _websocket_handler(scope, receive, send):
    """ASGI WebSocket handler for /api/player_positions_b/"""
    # Accept the WebSocket connection
    await send({"type": "websocket.accept", "subprotocol": "protobuf"})

    session = aiohttp.ClientSession(base_url=settings.MOD_SERVER_API_URL)
    try:
        while True:
            # Check if client disconnected
            try:
                message = await asyncio.wait_for(receive(), timeout=0.01)
                if message["type"] == "websocket.disconnect":
                    return
            except asyncio.TimeoutError:
                pass

            try:
                players = await get_players_mod_masked(session)
                data = serialize_players(players)
                await send({"type": "websocket.send", "bytes": data})
            except Exception:
                logger.exception("Error sending player positions")

            await asyncio.sleep(POSITION_UPDATE_SLEEP)
    finally:
        await session.close()


async def player_positions_ws_app(scope, receive, send):
    """Top-level ASGI app that handles WebSocket for player_positions_b."""
    if scope["type"] == "websocket":
        path = scope.get("path", "")
        # Match /api/player_positions_b/ or /api/player_positions_b
        if path.rstrip("/") == "/api/player_positions_b":
            await _websocket_handler(scope, receive, send)
            return
    # Not our route — signal to caller
    raise NotImplementedError("not player_positions_b ws route")
