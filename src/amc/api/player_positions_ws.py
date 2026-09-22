import asyncio
import logging

from amc.api.player_positions_pb2 import PlayerPositions, VehicleKey
from amc.api.positions_broadcaster import get_positions_broadcaster

logger = logging.getLogger(__name__)


_VEHICLE_KEY_MAP: dict[str, int] = {
    desc.name.replace("VEHICLE_KEY_", ""): val
    for val, desc in VehicleKey.DESCRIPTOR.values_by_number.items()
    if val != 0
}


def serialize_players(players: list[dict]) -> bytes:
    """Serialize a roster from get_players_mod_masked().

    The mask is the single source of truth: hidden=True entries already carry
    a zeroed Location and empty VehicleKey — this function does not re-apply
    any masking, it only translates the dict to protobuf.
    """
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
        enum_val = _VEHICLE_KEY_MAP.get(raw_key)
        if enum_val is not None:
            pos.vehicle_key_enum = enum_val
        else:
            pos.vehicle_key_unknown = raw_key
        pos.hidden = bool(p.get("hidden", False))
    return positions.SerializeToString()


async def _watch_disconnect(receive, disconnect: asyncio.Event):
    while True:
        message = await receive()
        if message["type"] == "websocket.disconnect":
            disconnect.set()
            return


async def _websocket_handler(scope, receive, send):
    """ASGI WebSocket handler for /api/player_positions_b/"""
    # Accept the WebSocket connection
    await send({"type": "websocket.accept", "subprotocol": "protobuf"})

    broadcaster = get_positions_broadcaster()
    await broadcaster.ensure_started()

    disconnect = asyncio.Event()
    watcher = asyncio.create_task(_watch_disconnect(receive, disconnect))
    try:
        async for players in broadcaster.stream_masked():
            if disconnect.is_set():
                break
            try:
                data = serialize_players(players)
                await send({"type": "websocket.send", "bytes": data})
            except Exception:
                logger.exception("Error sending player positions")
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass


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
