import asyncio
import logging

import aiohttp
from django.conf import settings

from amc.api.player_positions_common import POSITION_UPDATE_SLEEP, get_players_mod
from amc.api.player_positions_pb2 import PlayerPositions
from amc.api.vehicle_key_map import VEHICLE_KEY_MAP

logger = logging.getLogger(__name__)


def serialize_players(players: list[dict]) -> bytes:
    positions = PlayerPositions()
    for p in players:
        loc = p.get("Location", {})
        pos = positions.players.add()
        pos.unique_id = int(p.get("UniqueID", 0))
        pos.player_name = str(p.get("PlayerName", ""))
        pos.x = float(loc.get("X", 0))
        pos.y = float(loc.get("Y", 0))
        pos.z = float(loc.get("Z", 0))

        raw_key = str(p.get("VehicleKey", ""))
        enum_val = VEHICLE_KEY_MAP.get(raw_key)
        if enum_val is not None:
            pos.vehicle_key_enum = enum_val
        else:
            pos.vehicle_key_unknown = raw_key
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
                players = await get_players_mod(session)
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
