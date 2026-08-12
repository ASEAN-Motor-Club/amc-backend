import asyncio
import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone as dt_timezone

import aiohttp
from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.utils import timezone
from django.conf import settings

from amc.models import (
    Character,
    CharacterLocation,
    ShortcutZone,
    TeleportPortal,
)
from amc.utils import skip_if_running
from amc.mod_server import show_popup, teleport_player
from amc.game_server import get_players_with_location, get_players_locations

logger = logging.getLogger("amc.locations")

# Teleport detection via location delta (disabled by default — the mod server
# hooks (ServerTeleportCharacter etc.) are the correct detection mechanism.
# This hotfix fires false positives when wanted players drive at normal speed,
# as 10,000 units (100m) is easily exceeded between ticks.)
TELEPORT_DISTANCE_THRESHOLD = 100_000  # game units (~1km)
TELEPORT_DETECTION_WINDOW = 10  # minutes — match TELEPORT_PENALTY_WINDOW
LOCATION_TELEPORT_DETECTION_ENABLED = os.environ.get(
    "LOCATION_TELEPORT_DETECTION_ENABLED", "0"
).lower() in ("1", "true", "yes")

# Jail boundary enforcement
JAIL_BOUNDARY_RADIUS = 1_000   # 10 m (100 game units = 1 m)
JAIL_BOUNDARY_MESSAGE = """\
<Title>⛓️ Stay in Jail</>
<Warning>You are under arrest — you cannot leave jail!</>
"""


point_of_interests = [
    (
        Point(**{"z": -20696.78, "y": 150230.13, "x": 1025.73}),
        300,
        """\
<Title>Corporation Rules</>
<Warning>Corporations are NOT ALLOWED</> - if you are planning to use AI drivers.
Having too many AI vehicles on the server have caused traffic jams and other mishaps.
Unlicensed corporations will be closed down!

<Bold>You may ONLY start a corporation for the following purposes:</>
- Spawning Campy's around the map
- Renting out vehicles for other players
- Showcasing liveries for car shows

For any other purposes, <Highlight>please contact the admins on the discord</>.
""",
    ),
    (
        Point(**{"z": -21564.73, "y": 157275.61, "x": -83784.37}),
        2000,
        f"""\
<Title>Welcome to the ASEAN Park</>

{settings.CREDITS_TEXT}
""",
    ),
]

SHORTCUT_ZONE_WARNING_RADIUS = 2000  # game units (~20m)

# How recently a player must have been inside a shortcut zone (via
# shortcut_zone_entered_at) before we suppress the entry popup. Re-crossing
# the boundary within this window means they already saw the warning, so we
# don't spam the popup on every touch.
SHORTCUT_ZONE_ENTRY_POPUP_WINDOW = timedelta(minutes=2)

SHORTCUT_ZONE_WARNING_MESSAGE = """\
<Title>⚠️ Shortcut Zone Ahead</>
<Warning>You are near a shortcut zone!</>
Deliveries made through this area will <Highlight>NOT receive any subsidy bonus</> and will <Highlight>NOT count towards job completion</>.
"""

SHORTCUT_ZONE_ENTRY_MESSAGE = """\
<Title>⛔ Entered Shortcut Zone</>
<Warning>You are now INSIDE a shortcut zone!</>
Any delivery completed while having passed through this area will <Highlight>NOT be subsidised</> and will <Highlight>NOT count towards job completion</>.
"""


async def _check_shortcut_zones(character, old_location, new_location, ctx):
    """Warn players when they approach or enter a ShortcutZone.

    Also maintains ``character.shortcut_zone_entered_at`` — the last time the
    player was inside a shortcut zone. It is refreshed on every inside tick and
    **never cleared on exit**: webhook processing uses this as a rolling
    1-hour taint window (a delivery within 1h of a shortcut-zone pass gets no
    subsidy / job credit). The timestamp ages out on its own once stale.
    """
    player = character.player
    http_client_mod = ctx.get("http_client_mod")
    if http_client_mod is None:
        return

    old_2d = Point(old_location.x, old_location.y, srid=0)
    new_2d = Point(new_location.x, new_location.y, srid=0)

    async for zone in ShortcutZone.objects.filter(active=True):
        zone_geom = zone.polygon.clone()
        zone_geom.srid = 0  # match the player point SRID for distance calc

        distance_old = old_2d.distance(zone_geom)
        distance_new = new_2d.distance(zone_geom)

        # Proximity WARNING (e.g. 2000 units away)
        was_outside_warning = distance_old > SHORTCUT_ZONE_WARNING_RADIUS
        is_inside_warning = (
            distance_new <= SHORTCUT_ZONE_WARNING_RADIUS and distance_new > 0
        )

        if was_outside_warning and is_inside_warning:
            await show_popup(
                http_client_mod,
                SHORTCUT_ZONE_WARNING_MESSAGE,
                player_id=player.unique_id,
            )
            await asyncio.sleep(0.1)

        # Actual ENTRY (inside the polygon)
        was_outside_polygon = distance_old > 0
        is_inside_polygon = distance_new == 0

        if is_inside_polygon:
            # Capture the prior timestamp before refreshing below, so the
            # entry popup can be debounced against it.
            prev_entered_at = character.shortcut_zone_entered_at

            # Refresh the "last time inside a shortcut zone" timestamp on
            # every inside tick (not just the entry crossing) so the 1-hour
            # taint window tracks actual occupancy. This also covers cases
            # (spawn-in, teleport-in) where no boundary-crossing transition
            # ever fires. Never cleared on exit — webhook.py ages it out via
            # the `> now - 1h` check, giving the lingering taint the popup
            # text promises.
            character.shortcut_zone_entered_at = timezone.now()

            now = timezone.now()
            recently_inside = (
                prev_entered_at is not None
                and prev_entered_at > now - SHORTCUT_ZONE_ENTRY_POPUP_WINDOW
            )

            # Only show the entry popup on a real (re)entry — suppress it if
            # the player was already inside a shortcut zone very recently, so a
            # player drifting across the boundary doesn't get spammed.
            if was_outside_polygon and not recently_inside:
                await show_popup(
                    http_client_mod,
                    SHORTCUT_ZONE_ENTRY_MESSAGE,
                    player_id=player.unique_id,
                )
                await asyncio.sleep(0.1)


async def _check_jail_boundary(character, new_location, ctx):
    """Enforce the jail perimeter for recently arrested characters.

    If ``character.jailed_until`` is set the player is under arrest.  Two
    outcomes are possible on each call:

    1. **Time expired** — current time is past ``jailed_until`` → clear it
       and return.  The player is now free.
    2. **Out of bounds** — player is more than JAIL_BOUNDARY_RADIUS game units
       from the jail ``TeleportPoint`` → teleport them back and show a popup.
    """
    if not character.jailed_until:
        return

    # Auto-release when current time is past jailed_until
    from django.utils import timezone as _tz

    if _tz.now() >= character.jailed_until:
        character.jailed_until = None
        return

    http_client_mod = ctx.get("http_client_mod")
    if http_client_mod is None:
        return

    # Fetch jail TeleportPoint (cheap — only hit when character is jailed)
    from amc.models import TeleportPoint

    try:
        jail_tp = await TeleportPoint.objects.aget(name__iexact="jail")
    except TeleportPoint.DoesNotExist:
        logger.warning("Jail boundary check skipped — 'jail' TeleportPoint not found")
        return

    jail_point = jail_tp.location
    distance = new_location.distance(jail_point)

    if distance <= JAIL_BOUNDARY_RADIUS:
        return  # within bounds — nothing to do

    logger.info(
        "Jailed player %s strayed %.0f units from jail — teleporting back",
        character.name,
        distance,
    )

    jail_coords = {"X": jail_point.x, "Y": jail_point.y, "Z": jail_point.z}
    # Use player_id (FK integer) directly — avoids a sync DB access in async context
    player_uid = str(character.player_id)
    try:
        await teleport_player(
            http_client_mod,
            player_uid,
            jail_coords,
            no_vehicles=True,
            force=True,
        )
    except Exception:
        # Teleport failed (e.g. player already offline) — do not crash the monitor
        pass
    else:
        await show_popup(
            http_client_mod,
            JAIL_BOUNDARY_MESSAGE,
            player_id=player_uid,
        )


async def _check_pois_and_portals(character, old_location, new_location, ctx):
    """Check POI entries and portal triggers using the cached last_location."""
    player_id = character.player_id
    http_client_mod = ctx.get("http_client_mod")
    if http_client_mod is None:
        return

    for target_point, target_radius_meters, message in point_of_interests:
        distance_to_new = new_location.distance(target_point)
        distance_to_old = old_location.distance(target_point)

        was_outside = distance_to_old > target_radius_meters
        is_inside = distance_to_new <= target_radius_meters

        if was_outside and is_inside:
            await show_popup(http_client_mod, message, player_id=player_id)
            await asyncio.sleep(0.1)

    async for portal in TeleportPortal.objects.filter(active=True):
        source_point = portal.source
        target_point = portal.target
        source_radius_meters = portal.source_radius
        distance_to_new = new_location.distance(source_point)
        distance_to_old = old_location.distance(source_point)

        was_outside = distance_to_old > source_radius_meters
        is_inside = distance_to_new <= source_radius_meters

        if was_outside and is_inside:
            # Block wanted criminals from using portals — send to jail instead
            from amc.models import Wanted

            active_wanted = await Wanted.objects.filter(
                character=character,
                expired_at__isnull=True,
                wanted_remaining__gt=0,
            ).afirst()
            if active_wanted:
                logger.info(
                    "Wanted criminal %s entered portal — sending to jail",
                    character.name,
                )
                from amc.commands.teleport import _auto_arrest_wanted_criminal

                await _auto_arrest_wanted_criminal(
                    active_wanted,
                    character,
                    character.player,
                    http_client_mod,
                    reason="Arrested for using a teleport portal while wanted.",
                )
                await asyncio.sleep(0.1)
                continue

            await teleport_player(
                http_client_mod,
                str(player_id),
                {"X": target_point.x, "Y": target_point.y, "Z": target_point.z},
            )
            await asyncio.sleep(0.1)


async def _process_location_checks(ctx, players, has_telemetry):
    """Run real-time checks (jail, POIs, portals, shortcut zones) and build DB objects.

    Returns ``(new_locations, characters_to_update)`` — the caller is
    responsible for flushing these to the database (via ``_flush_locations_to_db``).
    """
    guid_to_player_info = {
        p["CharacterGuid"]: p for p in players if p.get("CharacterGuid")
    }
    if not guid_to_player_info:
        return [], []

    characters = {
        c.guid: c
        async for c in Character.objects.select_related("player").filter(
            guid__in=guid_to_player_info.keys()
        )
    }

    new_locations = []
    characters_to_update = []
    now = timezone.now()

    for guid, player_info in guid_to_player_info.items():
        character = characters.get(guid)
        if not character:
            continue

        location_data = player_info["Location"]
        new_point = Point(
            location_data["X"], location_data["Y"], location_data.get("Z", 0)
        )
        vehicle_key = player_info["VehicleKey"]

        await _check_jail_boundary(character, new_point, ctx)

        if character.last_location:
            await _check_pois_and_portals(
                character, character.last_location, new_point, ctx
            )
            await _check_shortcut_zones(
                character, character.last_location, new_point, ctx
            )

        loc_kwargs = {}
        if has_telemetry:
            vel = player_info.get("Velocity", {})
            loc_kwargs = {
                "yaw": player_info.get("Yaw"),
                "speed": player_info.get("Speed"),
                "velocity_x": vel.get("X"),
                "velocity_y": vel.get("Y"),
                "velocity_z": vel.get("Z"),
                "rpm": player_info.get("RPM"),
                "gear": player_info.get("Gear"),
            }

        capture_ts = player_info.get("_capture_ts")
        if capture_ts:
            loc_kwargs["timestamp"] = capture_ts

        new_locations.append(
            CharacterLocation(
                character=character,
                location=new_point,
                vehicle_key=vehicle_key,
                **loc_kwargs,
            )
        )
        character.last_location = new_point
        character.last_vehicle_key = vehicle_key
        character.last_online = now
        characters_to_update.append(character)

    return new_locations, characters_to_update


async def _flush_locations_to_db(new_locations, characters_to_update):
    """Persist accumulated location rows and character updates to the database."""
    if new_locations:
        await CharacterLocation.objects.abulk_create(
            new_locations, ignore_conflicts=True
        )

    if characters_to_update:
        await Character.objects.abulk_update(
            characters_to_update,
            [
                "last_location",
                "last_vehicle_key",
                "last_online",
                "shortcut_zone_entered_at",
                "jailed_until",
            ],
        )


async def _process_location_batch(ctx, players, has_telemetry):
    """Process a batch of player location data: checks + immediate DB flush."""
    new_locations, characters_to_update = await _process_location_checks(
        ctx, players, has_telemetry,
    )
    await _flush_locations_to_db(new_locations, characters_to_update)


def _transform_snapshots(snapshots):
    """Transform C++ mod SSE snapshots into the player-info dicts expected by _process_location_batch.

    Each snapshot has shape: {"timestamp_ms": N, "entries": [{"character_guid", "location": {x,y,z}, ...}]}
    Output shape per player: {"CharacterGuid", "Location": {X,Y,Z}, "VehicleKey", "Yaw", "Speed", "Velocity": {X,Y,Z}, "RPM", "Gear", "_capture_ts"}
    """
    result = []
    for snap in snapshots:
        ts_ms = snap.get("timestamp_ms", 0)
        capture_ts = (
            datetime.fromtimestamp(ts_ms / 1000.0, tz=dt_timezone.utc)
            if ts_ms > 0
            else None
        )
        for e in snap.get("entries", []):
            guid = e.get("character_guid", "").upper()
            if not guid:
                continue
            loc = e.get("location", {})
            vel = e.get("velocity", {})
            vx, vy, vz = vel.get("x", 0), vel.get("y", 0), vel.get("z", 0)
            speed = math.sqrt(vx * vx + vy * vy + vz * vz)
            vehicle_key = e.get("vehicle_key", "") or None
            result.append({
                "CharacterGuid": guid,
                "Location": {"X": loc["x"], "Y": loc["y"], "Z": loc["z"]},
                "VehicleKey": vehicle_key,
                "Yaw": e.get("yaw", 0),
                "Speed": speed,
                "Velocity": {"X": vx, "Y": vy, "Z": vz},
                "RPM": e.get("rpm", 0),
                "Gear": e.get("gear", 0),
                "_capture_ts": capture_ts,
            })
    return result


async def run_location_listener(ctx):
    """Long-running SSE consumer for /players/locations/stream.

    Probes the endpoint on startup — if unavailable, returns immediately and
    monitor_locations cron stays active.  Sets a Redis flag
    ``location_sse_active`` (TTL 30s) each iteration so monitor_locations can
    skip when SSE is capturing data.
    """
    base_url = settings.MOD_MANAGEMENT_API_URL
    http_client_mgmt = ctx.get("http_client_mgmt")
    if http_client_mgmt is None:
        return

    # Probe: check if the endpoint exists
    try:
        async with http_client_mgmt.get(
            "/players/locations/stream",
            timeout=aiohttp.ClientTimeout(total=3),
        ) as resp:
            if resp.status == 404:
                logger.info(
                    "Location SSE endpoint not available — monitor_locations cron stays active"
                )
                return
    except (aiohttp.ClientError, asyncio.TimeoutError):
        logger.info(
            "Location SSE probe failed — monitor_locations cron stays active"
        )
        return

    INITIAL_BACKOFF = 1
    MAX_BACKOFF = 30
    HEALTHY_SESSION_SECONDS = 60
    FLUSH_INTERVAL = 0.5
    DB_FLUSH_INTERVAL = 2.0
    SSE_TIMEOUT = aiohttp.ClientTimeout(
        total=None, sock_connect=10, sock_read=90,
    )

    backoff = INITIAL_BACKOFF
    event_loop = asyncio.get_event_loop()

    while True:
        connected_at = event_loop.time()
        try:
            async with aiohttp.ClientSession(
                base_url=base_url, timeout=SSE_TIMEOUT,
            ) as session:
                logger.info("Location SSE connecting to %s/players/locations/stream", base_url)

                async with session.get("/players/locations/stream") as resp:
                    if resp.status != 200:
                        logger.warning(
                            "Location SSE returned %s, retrying in %ss",
                            resp.status, backoff,
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, MAX_BACKOFF)
                        continue

                    logger.info("Location SSE connected")
                    connected_at = event_loop.time()
                    backoff = INITIAL_BACKOFF

                    event_buffer: list[dict] = []
                    current_lines: list[str] = []
                    last_flush = event_loop.time()
                    pending_locations: list = []
                    pending_characters: list = []
                    last_db_flush = event_loop.time()

                    try:
                        while True:
                            await cache.aset("location_sse_active", True, timeout=30)

                            try:
                                raw_line = await asyncio.wait_for(
                                    resp.content.readline(), timeout=120.0,
                                )
                            except asyncio.TimeoutError:
                                logger.warning(
                                    "Location SSE read timeout, forcing reconnect"
                                )
                                break

                            if not raw_line:
                                break

                            line = (
                                raw_line.decode("utf-8", errors="replace")
                                .rstrip("\n")
                                .rstrip("\r")
                            )

                            if line == "":
                                if current_lines:
                                    data_parts = [
                                        ln[5:].strip()
                                        for ln in current_lines
                                        if ln.startswith("data:")
                                    ]
                                    current_lines = []
                                    if data_parts:
                                        try:
                                            snap = json.loads(
                                                "\n".join(data_parts)
                                            )
                                            event_buffer.append(snap)
                                        except json.JSONDecodeError:
                                            pass

                                    now = event_loop.time()
                                    if (
                                        event_buffer
                                        and now - last_flush >= FLUSH_INTERVAL
                                    ):
                                        players = _transform_snapshots(event_buffer)
                                        event_buffer.clear()
                                        last_flush = now
                                        locs, chars = await _process_location_checks(
                                            ctx, players, has_telemetry=True,
                                        )
                                        pending_locations.extend(locs)
                                        pending_characters.extend(chars)

                                    if (
                                        pending_locations
                                        and now - last_db_flush >= DB_FLUSH_INTERVAL
                                    ):
                                        await _flush_locations_to_db(
                                            pending_locations, pending_characters,
                                        )
                                        pending_locations.clear()
                                        pending_characters.clear()
                                        last_db_flush = now
                            else:
                                current_lines.append(line)

                    finally:
                        if event_buffer:
                            players = _transform_snapshots(event_buffer)
                            event_buffer.clear()
                            locs, chars = await _process_location_checks(
                                ctx, players, has_telemetry=True,
                            )
                            pending_locations.extend(locs)
                            pending_characters.extend(chars)
                        if pending_locations:
                            await _flush_locations_to_db(
                                pending_locations, pending_characters,
                            )

        except asyncio.CancelledError:
            await cache.adelete("location_sse_active")
            logger.info("Location SSE listener shutting down")
            return

        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            session_duration = event_loop.time() - connected_at
            if session_duration >= HEALTHY_SESSION_SECONDS:
                backoff = INITIAL_BACKOFF
            logger.warning(
                "Location SSE error: %s, retrying in %ss", e, backoff,
            )

        except Exception:
            logger.exception(
                "Location SSE unexpected error, retrying in %ss", backoff,
            )

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)


@skip_if_running
async def monitor_locations(ctx):
    if await cache.aget("location_sse_active"):
        return

    http_client_mgmt = ctx.get("http_client_mgmt")
    players = None
    if http_client_mgmt:
        players = await get_players_locations(http_client_mgmt)

    if players is not None:
        await _process_location_batch(ctx, players, has_telemetry=True)
    else:
        http_client = ctx.get("http_client")
        players = await get_players_with_location(http_client)
        if not players:
            return
        await _process_location_batch(ctx, players, has_telemetry=False)
