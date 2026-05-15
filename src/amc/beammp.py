import asyncio
import json
import logging

logger = logging.getLogger(__name__)


async def query_beammp(host: str, port: int, timeout: float = 5.0) -> dict | None:
    """Query a BeamMP server via the TCP 'I' information packet.

    Returns a normalized dict with keys:
        players (int), max_players (int), player_names (list[str]),
        name, map, description, version, mods (list[str]), mods_total (int)
    Returns None on any failure.
    """
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(host, port)
            try:
                writer.write(b"I")
                await writer.drain()

                length_bytes = await reader.readexactly(4)
                length = int.from_bytes(length_bytes, byteorder="little")

                data_bytes = await reader.readexactly(length)
                raw = json.loads(data_bytes)
            finally:
                writer.close()
                await writer.wait_closed()

        player_names = [
            name for name in raw.get("playerslist", "").split(";") if name
        ]

        map_path = raw.get("map", "")
        if "/" in map_path:
            # "/levels/west_coast_usa/info.json" -> "west_coast_usa"
            parts = map_path.strip("/").split("/")
            map_name = parts[1] if len(parts) >= 3 else map_path
        else:
            map_name = map_path

        return {
            "players": int(raw.get("players", 0)),
            "max_players": int(raw.get("maxplayers", 0)),
            "player_names": player_names,
            "name": raw.get("name", "BeamMP Server"),
            "map": map_name,
            "description": raw.get("sdesc", ""),
            "version": raw.get("version", ""),
            "mods": [
                m for m in raw.get("modlist", "").split(";") if m
            ],
            "mods_total": int(raw.get("modstotal", 0)),
        }

    except Exception:
        logger.warning("BeamMP query failed for %s:%s", host, port, exc_info=True)
        return None
