import asyncio
import logging

import aiohttp
from django.conf import settings

from amc.api.player_positions_common import POSITION_UPDATE_SLEEP, PlayerPositionsSubscription
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
        pos = positions.players.add()
        pos.unique_id = int(p["unique_id"])
        pos.player_name = p["player_name"]
        pos.hidden = p["hidden"]
        pos.x = float(p["x"])
        pos.y = float(p["y"])
        pos.z = float(p["z"])

        raw_key = str(p["vehicle_key"])
        enum_val = _VEHICLE_KEY_MAP.get(raw_key)
        if enum_val is not None:
            pos.vehicle_key_enum = enum_val
        else:
            pos.vehicle_key_unknown = raw_key
    return positions.SerializeToString()


async def _websocket_handler(scope, receive, send):
    """ASGI WebSocket handler for /api/player_positions_b/"""
    await send({"type": "websocket.accept", "subprotocol": "protobuf"})

    session = aiohttp.ClientSession(base_url=settings.MOD_SERVER_API_URL)
    try:
        async for players in PlayerPositionsSubscription.subscribe(session):
            try:
                data = serialize_players(players)
                await send({"type": "websocket.send", "bytes": data})
            except Exception:
                logger.exception("Error sending player positions over WebSocket")

            # Check for disconnect non-blocking
            try:
                message = await asyncio.wait_for(receive(), timeout=0.001)
                if message["type"] == "websocket.disconnect":
                    return
            except asyncio.TimeoutError:
                pass
    finally:
        await session.close()


async def player_positions_ws_app(scope, receive, send):
    """Top-level ASGI app that handles WebSocket for player_positions_b."""
    if scope["type"] == "websocket":
        path = scope.get("path", "")
        if path.rstrip("/") == "/api/player_positions_b":
            await _websocket_handler(scope, receive, send)
            return
    raise NotImplementedError("not player_positions_b ws route")
