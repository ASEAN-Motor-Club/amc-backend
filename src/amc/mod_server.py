import asyncio
import aiohttp
from django.core.cache import cache
from amc.enums import VehicleKeyByLabel, VEHICLE_DATA

# TODO: Lua webserver handlers now return 503 when ExecuteInGameThreadSync times out
# (game thread too busy). Callers should handle aiohttp 503 responses gracefully —
# either retry with backoff or treat as a soft failure (log + skip) rather than
# raising an exception that aborts the enclosing event handler.

# Tighter timeout for high-frequency read-only monitoring endpoints
FAST_TIMEOUT = aiohttp.ClientTimeout(total=5)


async def show_popup(session, message, player_id=None, character_guid=None):
    await _write_limiter.acquire()
    params = {"message": message}
    if player_id is not None:
        params["playerId"] = str(player_id)
    if character_guid is not None:
        params["characterGuid"] = str(character_guid)
    await session.post("/messages/popup", json=params)


async def send_system_message(session, message, character_guid=None):
    await _write_limiter.acquire()
    params = {"message": message}
    params["characterGuid"] = str(character_guid)
    await session.post("/messages/system", json=params)


async def set_config(session, max_vehicles_per_player=12):
    await _write_limiter.acquire()
    params = {"MaxVehiclePerPlayer": max_vehicles_per_player}
    await session.post("/config", json=params)


async def set_character_name(session, character_guid, name):
    await _write_limiter.acquire()
    transfer = {
        "name": name,
    }
    async with session.put(f"/players/{character_guid}/name", json=transfer) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise Exception(
                f"Failed to change name (status={resp.status}, body={body[:200]})"
            )


async def transfer_money(session, amount, message, player_id):
    await _write_limiter.acquire()
    transfer = {
        "Amount": amount,
        "Message": message,
    }
    async with session.post(f"/players/{player_id}/money", json=transfer) as resp:
        if resp.status != 200:
            raise Exception("Failed to transfer money")


async def transfer_exp(session, player_id, level_type, exp, message=""):
    """Grant experience points to a player.

    Args:
        player_id: Player **unique ID** (NOT character GUID).
        level_type: EMTCharacterLevelType value (0=Driver, 1=Taxi, 2=Bus, 3=Truck, 4=Racer, 5=Wrecker, 6=Police).
        exp: Amount of experience to grant.
        message: Optional chat message shown to the player.
    """
    await _write_limiter.acquire()
    data = {
        "LevelType": level_type,
        "Exp": exp,
        "Message": message,
    }
    async with session.post(f"/players/{player_id}/exp", json=data) as resp:
        if resp.status != 200:
            raise Exception("Failed to transfer exp")


async def toggle_rp_session(session, player_guid, despawn=False):
    await _write_limiter.acquire()
    data = {"despawn": despawn}
    async with session.post(f"/rp_sessions/{player_guid}/toggle", json=data) as resp:
        if resp.status != 200:
            raise Exception("Failed to toggle RP session")


async def join_player_to_event(session, event_guid, player_id):
    await _write_limiter.acquire()
    data = {
        "PlayerId": player_id,
    }
    async with session.post(f"/events/{event_guid}/join", json=data) as resp:
        if resp.status != 204:
            raise Exception("Failed to join event")


async def kick_player_from_event(session, event_guid, player_id):
    await _write_limiter.acquire()
    data = {
        "PlayerId": player_id,
    }
    async with session.post(f"/events/{event_guid}/leave", json=data) as resp:
        if resp.status != 204:
            raise Exception("Failed to kick player from event")


async def get_events(session):
    async with session.get("/events", timeout=FAST_TIMEOUT) as resp:
        if resp.status != 200:
            raise Exception("Failed to fetch events")
        data = await resp.json()
        return data["data"]


async def list_player_vehicles(session, player_id, active=None, complete=None):
    # DISABLED: endpoint /player_vehicles/{id}/list is buggy and crashes the server.
    # Return empty dict to match the expected return type used by callers.
    return {}


async def send_message_as_player(session, message, player_id, category=None):
    await _write_limiter.acquire()
    data = {
        "Message": message,
    }
    if category is not None:
        data["Category"] = category
    async with session.post(f"/players/{player_id}/chat", json=data) as resp:
        if resp.status != 204:
            raise Exception("Failed to send message")


async def teleport_player(
    session,
    player_id,
    location,
    rotation=None,
    no_vehicles=False,
    reset_trailers=None,
    reset_carried_vehicles=None,
    force=False,
):
    await _write_limiter.acquire()
    data = {
        "Location": location,
    }
    if no_vehicles:
        data["NoVehicles"] = True
    if force:
        data["Force"] = True
    if reset_trailers is not None:
        data["bResetTrailers"] = reset_trailers
    if reset_carried_vehicles is not None:
        data["bResetCarriedVehicles"] = reset_carried_vehicles
    if rotation:
        data["Rotation"] = rotation
    async with session.post(f"/players/{player_id}/teleport", json=data) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise Exception(
                f"Failed to teleport player (status={resp.status}, body={body[:200]})"
            )


async def spawn_dealership(session, vehicle_key, location, yaw):
    await _write_limiter.acquire()
    data = {
        "Location": location,
        "Rotation": {"Roll": 0.0, "Pitch": 0.0, "Yaw": yaw},
        "VehicleClass": "",
        "VehicleParam": {
            "VehicleKey": vehicle_key,
        },
    }
    async with session.post("/dealers/spawn", json=data) as resp:
        if resp.status >= 400:
            raise Exception("Failed to spawn dealership")


_NEG_SENTINEL = "__none__"
_inflight: dict[str, asyncio.Future] = {}
_inflight_vehicle: dict[str, asyncio.Future] = {}


async def _coalesce(key: str, store: dict[str, asyncio.Future], coro_fn):
    """Share a single in-flight Future when multiple callers request the same key concurrently.

    Follows the same pattern as get_player's _inflight deduplication.
    """
    pending = store.get(key)
    if pending is not None:
        return await pending

    future = asyncio.get_running_loop().create_future()
    store[key] = future
    try:
        result = await coro_fn()
        future.set_result(result)
        return result
    except Exception as exc:
        if not future.done():
            future.set_exception(exc)
            # Mark the exception as retrieved so asyncio doesn't log a warning
            # when no concurrent callers are awaiting this future.  Concurrent
            # callers that *are* awaiting will still see the exception via
            # future.result() / __await__.
            future.exception()
        raise
    finally:
        store.pop(key, None)


async def get_player(session, player_id, force_refresh=False):
    cache_key = f"mod_player_info:{player_id}"
    if not force_refresh:
        cached = await cache.aget(cache_key)
        if cached is not None:
            return None if cached == _NEG_SENTINEL else cached

    pending = _inflight.get(player_id)
    if pending is not None:
        return await pending

    future = asyncio.get_running_loop().create_future()
    _inflight[player_id] = future
    try:
        async with session.get(f"/players/{player_id}") as resp:
            result = None
            if resp.status == 200:
                data = await resp.json()
                if data and data.get("data"):
                    result = data["data"][0]
            if result is None:
                await cache.aset(cache_key, _NEG_SENTINEL, timeout=5)
            else:
                await cache.aset(cache_key, result, timeout=5)
            future.set_result(result)
            return result
    except Exception as exc:
        if not future.done():
            future.set_exception(exc)
            future.exception()
        raise
    finally:
        _inflight.pop(player_id, None)


_MOD_PLAYERS_LIST_TTL = 2
_MOD_STATUS_TTL = 2
_MOD_PARTIES_TTL = 2
_MOD_PARTIES_CACHE_KEY = "mod_parties_list"
_MOD_STATUS_CACHE_KEY = "mod_status_general"


async def get_players(session):
    cached = cache.get("mod_players_list_all")
    if cached is not None:
        return cached

    async with session.get("/players") as resp:
        data = await resp.json()
        if not data or not data.get("data"):
            return None
        result = data["data"]
        cache.set("mod_players_list_all", result, timeout=_MOD_PLAYERS_LIST_TTL)
        return result


async def get_parties(session):
    """Fetch current parties. Returns empty list on failure (graceful degradation)."""
    cached = cache.get(_MOD_PARTIES_CACHE_KEY)
    if cached is not None:
        return cached

    try:
        async with session.get("/parties", timeout=FAST_TIMEOUT) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            result = data.get("data", [])
            cache.set(_MOD_PARTIES_CACHE_KEY, result, timeout=_MOD_PARTIES_TTL)
            return result
    except Exception:
        return []


def get_party_size_for_character(parties, character_guid):
    """Return party size for a character. Returns 1 if not in any party.
    character_guid should be an uppercase hex string (GuidToString format)."""
    guid_str = str(character_guid).upper()
    for party in parties:
        if guid_str in party.get("Players", []):
            return len(party["Players"])
    return 1


def get_party_members_for_character(parties, character_guid):
    """Return list of all party member GUIDs for a character's party.
    Returns [character_guid] if not in any party."""
    if not character_guid:
        return [character_guid] if character_guid else []
    guid_str = str(character_guid).upper()
    for party in parties:
        players = party.get("Players", [])
        if guid_str in players:
            return list(players)
    return [guid_str]


async def get_webhook_events(session):
    async with session.get("/webhook") as resp:
        data = await resp.json()
        return data


async def get_webhook_events2(session):
    # Note: ?since= filtering is not used because the C++ EventsRoute
    # rejects URLs with query params (exact match bug). Deduplication
    # is handled downstream by process_events' seq-based filtering.
    async with session.get("/events", timeout=FAST_TIMEOUT) as resp:
        data = await resp.json()
        return data.get("events", [])


async def get_status(session):
    cached = cache.get(_MOD_STATUS_CACHE_KEY)
    if cached is not None:
        return cached

    async with session.get("/status/general", timeout=FAST_TIMEOUT) as resp:
        data = await resp.json()
        if not data or not data.get("data"):
            return None
        result = data["data"]
        cache.set(_MOD_STATUS_CACHE_KEY, result, timeout=_MOD_STATUS_TTL)
        return result


async def get_rp_mode(session, player_id):
    return False


_patrol_payments_cache: dict = {}
_patrol_payments_cache_ts: float = 0


class _WriteRateLimiter:
    """Ensures a minimum gap between write (POST/PUT/DELETE) requests."""

    def __init__(self, min_interval_ms: float = 500):
        self._lock = asyncio.Lock()
        self._min_interval = min_interval_ms / 1000
        self._last_request_time = 0.0

    async def acquire(self):
        async with self._lock:
            now = asyncio.get_running_loop().time()
            elapsed = now - self._last_request_time
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
            self._last_request_time = asyncio.get_running_loop().time()


_write_limiter = _WriteRateLimiter(min_interval_ms=500)


async def get_patrol_point_payments(session, cache_ttl=300):
    """Fetch patrol point payment data from the mod server, cached for cache_ttl seconds."""
    import time

    global _patrol_payments_cache, _patrol_payments_cache_ts
    now = time.monotonic()
    if _patrol_payments_cache and (now - _patrol_payments_cache_ts) < cache_ttl:
        return _patrol_payments_cache

    try:
        async with session.get("/police/patrol_areas", timeout=FAST_TIMEOUT) as resp:
            if resp.status != 200:
                return _patrol_payments_cache
            data = await resp.json()
    except Exception:
        return _patrol_payments_cache

    payments = {}
    for area in data.get("data", []):
        for pt in area.get("Points", []):
            payments[pt["PatrolPointId"]] = {
                "BasePayment": pt.get("BasePayment", 0),
                "AreaBonusPayment": pt.get("AreaBonusPayment", 0),
            }

    _patrol_payments_cache = payments
    _patrol_payments_cache_ts = now
    return payments


async def get_decal(session, player_id):
    async with session.get(f"/player_vehicles/{player_id}/decal") as resp:
        if resp.status != 200:
            raise Exception("Failed to get decal")
        data = await resp.json()
        return data


async def set_decal(session, player_id, decal):
    await _write_limiter.acquire()
    async with session.post(f"/player_vehicles/{player_id}/decal", json=decal) as resp:
        if resp.status != 200:
            raise Exception("Failed to set decal")


async def get_player_customization(session, character_guid):
    async with session.get(
        f"/players/{character_guid}/customization", timeout=FAST_TIMEOUT
    ) as resp:
        if resp.status == 404:
            return None
        if resp.status != 200:
            raise Exception(f"Failed to get customization ({resp.status})")
        data = await resp.json()
        return data.get("data")


async def get_player_last_vehicle(session, character_guid):
    async def _fetch():
        async with session.get(
            f"/player_vehicles/{character_guid}/last", timeout=FAST_TIMEOUT
        ) as resp:
            if resp.status == 404:
                return {"vehicle": None}
            if resp.status != 200:
                raise Exception(f"Failed to get player last vehicle ({resp.status})")
            return await resp.json()

    return await _coalesce(f"last_vehicle:{character_guid}", _inflight_vehicle, _fetch)


async def get_player_last_vehicle_decals(session, character_guid):
    async with session.get(
        f"/player_vehicles/{character_guid}/last/decals", timeout=FAST_TIMEOUT
    ) as resp:
        if resp.status != 200:
            raise Exception("Failed to get player last vehicle decals")
        return await resp.json()


async def get_player_last_vehicle_parts(session, character_guid, complete=False):
    url = f"/player_vehicles/{character_guid}/last/parts"
    if complete:
        url += "?complete=1"
    cache_key = f"last_vehicle_parts:{character_guid}:{complete}"

    async def _fetch():
        async with session.get(url, timeout=FAST_TIMEOUT) as resp:
            if resp.status == 404:
                return {"parts": []}
            if resp.status != 200:
                raise Exception(f"Failed to get player last vehicle parts ({resp.status})")
            return await resp.json()

    return await _coalesce(cache_key, _inflight_vehicle, _fetch)


async def despawn_player_vehicle(session, player_id, category="current"):
    await _write_limiter.acquire()
    if category == "others":
        json = {"others": True}
    elif category == "all":
        json = {"all": True}
    else:
        json = {}
    async with session.post(f"/player_vehicles/{player_id}/despawn", json=json) as resp:
        if resp.status != 200:
            raise Exception("Failed to despawn")


async def despawn_spawned_vehicles(session, player_id, company_filter=None, ai_filter=None):
    await _write_limiter.acquire()
    data = {}
    if company_filter is not None:
        data["companyFilter"] = company_filter
    if ai_filter is not None:
        data["aiFilter"] = ai_filter
    async with session.post(f"/player_vehicles/{player_id}/despawn_spawned", json=data) as resp:
        if resp.status not in (200, 404):
            raise Exception("Failed to despawn spawned vehicles")


async def force_exit_vehicle(session, character_guid):
    async with session.get(f"/player_vehicles/{character_guid}/exit") as resp:
        if resp.status != 200:
            raise Exception("Failed to exit vehicle")


async def enter_last_vehicle(session, character_guid):
    await _write_limiter.acquire()
    async with session.post(f"/players/{character_guid}/enter_last_vehicle") as resp:
        data = await resp.json()
        if resp.status != 200:
            return {"error": data.get("error", "Unknown error")}
        return {"status": "success"}


async def make_suspect(session, character_guid, duration_seconds=300):
    await _write_limiter.acquire()
    async with session.post(
        f"/players/{character_guid}/suspect",
        json={"DurationSeconds": duration_seconds},
    ) as resp:
        if resp.status not in (200, 204):
            body = await resp.text()
            raise Exception(f"Failed to make suspect (status={resp.status}, body={body[:200]})")
        if resp.status == 200:
            return await resp.json()
        return None


async def remove_event(session, event_guid):
    await _write_limiter.acquire()
    async with session.delete(f"/events/{event_guid}") as resp:
        if resp.status not in (200, 204):
            body = await resp.text()
            raise Exception(f"Failed to remove event {event_guid} (status={resp.status}, body={body[:200]})")


async def clear_suspect(session, character_guid):
    """Remove every active UGE_PoliceSuspect_C gameplay effect from the player.

    Inverse of ``make_suspect``.  Safe to call on a non-suspect — the GAS
    remove-by-source API is a no-op when no matching GE is active.  The GE's
    on-remove lifecycle tears down the ``Net_Suspects`` entry and the granted
    suspect GameplayTags automatically.
    """
    await _write_limiter.acquire()
    async with session.delete(f"/players/{character_guid}/suspect") as resp:
        if resp.status not in (200, 204):
            body = await resp.text()
            raise Exception(f"Failed to clear suspect (status={resp.status}, body={body[:200]})")
        if resp.status == 200:
            return await resp.json()
        return None


async def despawn_player_cargo(session, character_guid):
    await _write_limiter.acquire()
    async with session.post(f"/players/{character_guid}/despawn_cargo") as resp:
        if resp.status != 200:
            raise Exception("Failed to despawn cargo")
        return await resp.json()


async def get_vehicle_cargos(session, character_guid):
    """Fetch cargos loaded on the player's current vehicle (and trailer chain).
    Returns a list of vehicle dicts, each with a list of cargoSpaces containing
    cargos. Returns None if the player has no vehicle or the request fails."""
    async with session.get(
        f"/players/{character_guid}/vehicle_cargos", timeout=FAST_TIMEOUT
    ) as resp:
        if resp.status == 404:
            return None
        if resp.status != 200:
            raise Exception(f"Failed to get vehicle cargos (status={resp.status})")
        data = await resp.json()
        return data.get("data")


async def set_world_vehicle_decal(
    session,
    vehicle_class,
    customization=None,
    decal=None,
    parts=None,
):
    await _write_limiter.acquire()
    data = {}
    if customization:
        data["customization"] = customization
    if decal:
        data["decal"] = decal
    if parts:
        data["parts"] = parts
    async with session.post(
        f"/world_vehicles/{vehicle_class}/decal", json=data
    ) as resp:
        if resp.status not in (200, 204):
            raise Exception("Failed to set vehicle decals")


async def spawn_assets(session, assets):
    await _write_limiter.acquire()
    data = assets
    async with session.post("/assets/spawn", json=data) as resp:
        if resp.status != 200:
            raise Exception("Failed to spawn asset")


async def despawn_by_tag(session, tag):
    await _write_limiter.acquire()
    data = {"Tag": tag, "Tags": []}
    async with session.post("/assets/despawn", json=data) as resp:
        if resp.status != 204:
            raise Exception("Failed to despawn by tag")


async def get_garages(session):
    async with session.get("/garages") as resp:
        data = await resp.json()
        if not data or not data.get("data"):
            return []
        return data["data"]


async def spawn_garage(
    session,
    location,
    rotation,
):
    await _write_limiter.acquire()
    data = {"Location": location, "Rotation": rotation}
    async with session.post("/garages/spawn", json=data) as resp:
        if resp.status != 201:
            raise Exception("Failed to spawn garage")
        data = await resp.json()
        return data["data"]


async def spawn_vehicle(
    session,
    vehicle_label,
    location,
    rotation={},
    customization=None,
    decal=None,
    parts=None,
    driver_guid=None,
    tag="amc",
    extra_data={},
):
    await _write_limiter.acquire()
    try:
        vehicle_key = VehicleKeyByLabel.get(vehicle_label)
        if not vehicle_key:
            raise Exception(f"Vehicle {vehicle_label} not found")

        vehicle_data = VEHICLE_DATA.get(vehicle_key)
        if not vehicle_data:
            raise Exception(f"Vehicle data for key {vehicle_key} not found")
        asset_path = vehicle_data["asset_path"]
    except Exception:
        asset_path = vehicle_label

    data = {
        "Location": location,
        "Rotation": rotation,
        "AssetPath": asset_path,
        "tag": tag,
        **extra_data,
    }
    if driver_guid:
        data["driverGuid"] = driver_guid
    if customization:
        data["customization"] = customization
    if decal:
        data["decal"] = decal
    if parts:
        data["parts"] = parts

    async with session.post("/vehicles/spawn", json=data) as resp:
        if resp.status == 503:
            raise Exception("Failed to spawn vehicle: server busy (503)")
        if resp.status != 200:
            raise Exception("Failed to spawn vehicle")


async def mute_player(session, player_id, mute_for=True, hard=True):
    await _write_limiter.acquire()
    data = {
        "MuteFor": mute_for,
        "Hard": hard,
    }
    async with session.post(f"/players/{player_id}/mute", json=data) as resp:
        if resp.status != 200:
            raise Exception("Failed to mute player")
        return await resp.json()


async def unmute_player(session, player_id):
    await _write_limiter.acquire()
    async with session.delete(f"/players/{player_id}/mute") as resp:
        if resp.status != 200:
            raise Exception("Failed to unmute player")
        return await resp.json()


async def get_muted_players(session):
    async with session.get("/players/muted", timeout=FAST_TIMEOUT) as resp:
        if resp.status != 200:
            return []
        data = await resp.json()
        return data.get("data", [])


async def get_vehicle_parts_by_tag(session, tag):
    """Poll mod server for parts status of a tagged vehicle."""
    async with session.get(f"/vehicle_parts_by_tag/{tag}", timeout=FAST_TIMEOUT) as resp:
        if resp.status != 200:
            return None
        return await resp.json()


async def set_vehicle_parts_by_tag(session, tag, parts):
    """Re-apply parts to an existing tagged vehicle."""
    await _write_limiter.acquire()
    data = {"parts": parts}
    async with session.post(f"/vehicle_parts_by_tag/{tag}", json=data) as resp:
        if resp.status != 200:
            raise Exception(f"Failed to set parts on vehicle {tag}")
        return await resp.json()


async def create_event(session, event_name, race_setup_config, event_guid=None):
    """Create a player-less event on the mod server.

    Transforms the RaceSetup.config into the format expected by the mod API
    (waypoints use Translation instead of Location).
    """
    import uuid

    await _write_limiter.acquire()
    config = dict(race_setup_config)
    config["Route"]["Waypoints"] = [
        {
            "Translation": wp["Location"],
            "Scale3D": wp["Scale3D"],
            "Rotation": wp["Rotation"],
        }
        for wp in config["Route"]["Waypoints"]
    ]
    if not config.get("VehicleKeys"):
        config["VehicleKeys"] = []
    if not config.get("EngineKeys"):
        config["EngineKeys"] = []

    data = {
        "EventGuid": event_guid or str(uuid.uuid4()).replace("-", "").upper(),
        "EventName": event_name,
        "EventType": 1,
        "RaceSetup": config,
    }
    async with session.post("/events", json=data) as resp:
        if resp.status >= 400:
            error_body = await resp.text()
            raise Exception(
                f"Failed to create event (status={resp.status}, body={error_body[:200]})"
            )
        return data["EventGuid"]
