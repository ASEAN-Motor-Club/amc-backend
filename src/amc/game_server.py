import asyncio
import logging
import math
from typing import Any, cast
import urllib.parse
import aiohttp
from yarl import URL
from django.core.cache import cache

logger = logging.getLogger(__name__)

_NEG_SENTINEL = "__none__"


async def game_api_request(
    session, url, method="get", password="", params={}, timeout=15
):
    req_params = {"password": password, **params}
    params_str = urllib.parse.urlencode(
        req_params, quote_via=cast(Any, urllib.parse.quote)
    )
    try:
        fn = getattr(session, method)
    except AttributeError as e:
        print(f"Invalid method: {e}")
        raise e

    request_timeout = aiohttp.ClientTimeout(total=timeout)
    async with fn(
        URL(f"{url}?{params_str}", encoded=True), timeout=request_timeout
    ) as resp:
        resp_json = await resp.json()
        return resp_json


async def get_players(session, password="", force_refresh=False):
    cache_key = "game_online_players_list"
    if not force_refresh:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    data = await game_api_request(session, "/player/list")
    if not data or "data" not in data:
        return []
    players = [
        (player["unique_id"], player)
        for player in data["data"].values()
        if player is not None
    ]
    cache.set(cache_key, players, timeout=1)
    return players


def _parse_location_string(loc_str):
    """Parse native game API location string (e.g. 'X=123.4 Y=567.8 Z=-910.1') into a dict."""
    result = {}
    for part in loc_str.split():
        axis, _, value = part.partition("=")
        result[axis] = float(value)
    return result


async def get_players_with_location(session):
    """Return player location data from the native game API.

    Returns a list of dicts with the same keys that monitor_locations expects:
      CharacterGuid, Location {x, y, z}, VehicleKey, UniqueID, PlayerName

    Uses the native /player/list endpoint (no game-thread pressure) with a 1s
    Redis cache, replacing the Lua mod server GET /players for location polling.
    """
    cache_key = "game_players_with_location"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    data = await game_api_request(session, "/player/list")
    if not data.get("succeeded") or "data" not in data:
        return []

    from amc.enums import VehicleKeyByLabel

    result = []
    for player in data["data"].values():
        if player is None:
            continue
        loc_str = player.get("location", "")
        if not loc_str:
            continue
        try:
            location = _parse_location_string(loc_str)
        except (ValueError, AttributeError):
            continue

        vehicle_info = player.get("vehicle")
        vehicle_name = vehicle_info["name"] if vehicle_info else None
        # Map display name → DB key; fall back to raw name for unlisted vehicles
        vehicle_key = VehicleKeyByLabel.get(vehicle_name, vehicle_name) if vehicle_name else "None"

        result.append({
            "CharacterGuid": player["character_guid"].upper(),
            "UniqueID": player["unique_id"],
            "PlayerName": player["name"],
            "Location": location,
            "VehicleKey": vehicle_key,
        })

    cache.set(cache_key, result, timeout=1)
    return result


_FALLBACK_SENTINEL = "__fallback__"


async def get_players_locations(session, use_cache: bool = True):
    """Fetch from /players/locations on the C++ mod management API.

    Returns None if the endpoint is unavailable (cached 30s to avoid retries).
    Returns a list of dicts with telemetry data when available.

    The 1s success cache exists for slower consumers; position streaming
    ticks faster than that TTL and must pass use_cache=False or it replays
    duplicate snapshots with fresh timestamps.
    """
    cache_key = "mod_players_locations"
    if use_cache:
        cached = cache.get(cache_key)
        if cached is not None:
            return None if cached == _FALLBACK_SENTINEL else cached

    try:
        async with session.get(
            "/players/locations",
            timeout=aiohttp.ClientTimeout(total=3),
        ) as resp:
            if resp.status == 404:
                cache.set(cache_key, _FALLBACK_SENTINEL, timeout=30)
                return None
            data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        cache.set(cache_key, _FALLBACK_SENTINEL, timeout=30)
        return None

    result = []
    for e in data.get("entries", []):
        loc = e.get("location", {})
        vel = e.get("velocity", {})
        vx, vy, vz = vel.get("x", 0), vel.get("y", 0), vel.get("z", 0)
        speed = math.sqrt(vx * vx + vy * vy + vz * vz)
        vehicle_key = e.get("vehicle_key", "") or None
        result.append(
            {
                "CharacterGuid": e["character_guid"].upper(),
                "Location": {"X": loc["x"], "Y": loc["y"], "Z": loc["z"]},
                "VehicleKey": vehicle_key,
                "Yaw": e.get("yaw", 0),
                "Speed": speed,
                "Velocity": {"X": vx, "Y": vy, "Z": vz},
                "RPM": e.get("rpm", 0),
                "Gear": e.get("gear", 0),
            }
        )
    cache.set(cache_key, result, timeout=1)
    return result


async def get_admins(session, password=""):
    """Fetch the current admin list from the game server.

    Returns a set of unique_id strings.  Cached for 60s since admin
    changes are infrequent.
    """
    cache_key = "game_admin_list"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        data = await game_api_request(session, "/player/role/list", params={"role": "admin"})
    except Exception:
        logger.warning("Failed to fetch admin list from game server", exc_info=True)
        return set()

    admin_set = set()
    if data and (data.get("succeeded") or "data" in data):
        admin_dict = data.get("data", {}).get("admin", {})
        for entry in admin_dict.values():
            if entry and entry.get("unique_id"):
                admin_set.add(entry["unique_id"])

    cache.set(cache_key, admin_set, timeout=60)
    return admin_set


async def get_player_info(session, unique_id, password="", force_refresh=False):
    """Return normalized player info from the native game API.

    The returned dict mirrors the shape of ``mod_server.get_player``:
      CharacterGuid, PlayerName, Location, VehicleKey, bIsAdmin, unique_id

    Cached with a 2s TTL to avoid hammering the game server.
    """
    cache_key = f"game_player_info:{unique_id}"
    if not force_refresh:
        cached = cache.get(cache_key)
        if cached is not None:
            return None if cached == _NEG_SENTINEL else cached

    data = await game_api_request(session, "/player/list")
    if not data or not (data.get("succeeded") or "data" in data):
        return None

    player = None
    for p in data.get("data", {}).values():
        if p is not None and str(p.get("unique_id")) == str(unique_id):
            player = p
            break

    if player is None:
        cache.set(cache_key, _NEG_SENTINEL, timeout=2)
        return None

    from amc.enums import VehicleKeyByLabel

    loc_str = player.get("location", "")
    location = _parse_location_string(loc_str) if loc_str else None

    vehicle_info = player.get("vehicle")
    vehicle_name = vehicle_info["name"] if vehicle_info else None
    vehicle_key = VehicleKeyByLabel.get(vehicle_name, vehicle_name) if vehicle_name else "None"

    admin_set = await get_admins(session, password=password)
    result = {
        "CharacterGuid": player["character_guid"].upper(),
        "PlayerName": player["name"],
        "Location": location,
        "VehicleKey": vehicle_key,
        "bIsAdmin": player["unique_id"] in admin_set,
        "unique_id": player["unique_id"],
    }

    cache.set(cache_key, result, timeout=2)
    return result


async def is_player_online(player_id, session, password=""):
    players = await get_players(session, password)
    player_ids = {str(player_id) for player_id, _ in players}
    return str(player_id) in player_ids


async def announcement_request(
    message, session, password="", type="message", color=None
):
    params = {"message": message}
    if type:
        params["type"] = type
    if color is not None:
        params["color"] = color
    return await game_api_request(session, "/chat", method="post", params=params)


async def announce(
    message: str,
    session,
    password="",
    clear_banner=True,
    type="message",
    color="FFFF00",
    delay=0,
):
    if delay > 0:
        await asyncio.sleep(delay)
    message_sanitized = message.strip().replace("\n", " ")
    try:
        await announcement_request(
            message_sanitized, session, password, type=type, color=color
        )
        if type == "announce" and clear_banner:
            await announcement_request(" ", session, password)
    except Exception as e:
        print(f"Error sending message: {e}")
        raise e


async def get_deliverypoints(session, password=""):
    return await game_api_request(session, "/delivery/sites")


async def get_world(session, password=""):
    return await game_api_request(
        session, "https://server.aseanmotorclub.com/api/world/"
    )


async def kick_player(unique_id, session):
    params = {
        "unique_id": unique_id,
    }
    return await game_api_request(session, "/player/kick", method="post", params=params)


async def ban_player(session, unique_id, hours=None, reason=None):
    params = {
        "unique_id": unique_id,
    }
    if hours:
        params["hours"] = hours
    if reason:
        params["reason"] = reason
    return await game_api_request(session, "/player/ban", method="post", params=params)


async def add_player_role(session, unique_id, role):
    params = {"unique_id": unique_id, "role": role}
    return await game_api_request(
        session, "/player/role/add", method="post", params=params
    )


async def remove_player_role(session, unique_id, role):
    params = {"unique_id": unique_id, "role": role}
    return await game_api_request(
        session, "/player/role/remove", method="post", params=params
    )
