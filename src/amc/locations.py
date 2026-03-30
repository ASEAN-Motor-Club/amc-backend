import asyncio
import logging
import os
from datetime import timedelta

from django.contrib.gis.geos import Point
from django.utils import timezone
from django.conf import settings

from amc.models import Character, CharacterLocation, Delivery, PoliceSession, ShortcutZone
from amc.utils import skip_if_running
from amc.mod_server import show_popup, teleport_player

logger = logging.getLogger("amc.locations")

# Teleport detection via location delta
TELEPORT_DISTANCE_THRESHOLD = 10_000  # game units (~100m)
TELEPORT_DETECTION_WINDOW = 10  # minutes — match TELEPORT_PENALTY_WINDOW
LOCATION_TELEPORT_DETECTION_ENABLED = os.environ.get(
    "LOCATION_TELEPORT_DETECTION_ENABLED", "1"
).lower() in ("1", "true", "yes")


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

portals = [
    # Meehoi house
    (
        Point(**{"x": 69664.27, "y": 651361.93, "z": -8214.26}),
        150,
        Point(**{"x": 68205.77, "y": 651084.19, "z": -7000.43}),
    ),
    (
        Point(**{"x": 68119.18, "y": 650502.15, "z": -6909.83}),
        120,
        Point(**{"x": 67912.23, "y": 650236.37, "z": -8512.19}),
    ),
    # Rooftop Bar
    (
        Point(**{"x": -67173.12, "y": 150561.7, "z": -20646.4}),
        150,
        Point(**{"x": -66531.100038674, "y": 150471.72884842, "z": -19706.865}),
    ),
    (
        Point(**{"x": -66733.74, "y": 150411.51, "z": -19703.15}),
        120,
        Point(**{"x": -67245.74, "y": 150831.6, "z": -20646.85}),
    ),
]

SHORTCUT_ZONE_WARNING_RADIUS = 2000  # game units (~20m)

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

    Also maintains ``character.shortcut_zone_entered_at`` — a timestamp set on
    zone entry, used by webhook processing to deny subsidies.  The timestamp
    auto-expires after 1 hour so the penalty doesn't stick forever.
    """
    player = character.player
    http_client_mod = ctx.get("http_client_mod")
    if http_client_mod is None:
        return

    old_2d = Point(old_location.x, old_location.y, srid=0)
    new_2d = Point(new_location.x, new_location.y, srid=0)

    currently_inside_any = False

    async for zone in ShortcutZone.objects.filter(active=True):
        zone_geom = zone.polygon.clone()
        zone_geom.srid = 0  # match the player point SRID for distance calc

        distance_old = old_2d.distance(zone_geom)
        distance_new = new_2d.distance(zone_geom)

        # Proximity WARNING (e.g. 2000 units away)
        was_outside_warning = distance_old > SHORTCUT_ZONE_WARNING_RADIUS
        is_inside_warning = distance_new <= SHORTCUT_ZONE_WARNING_RADIUS and distance_new > 0

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
            currently_inside_any = True

        if was_outside_polygon and is_inside_polygon:
            character.shortcut_zone_entered_at = timezone.now()
            await show_popup(
                http_client_mod,
                SHORTCUT_ZONE_ENTRY_MESSAGE,
                player_id=player.unique_id,
            )
            await asyncio.sleep(0.1)

    # Clear the timestamp when the player is confirmed outside ALL zones
    if character.shortcut_zone_entered_at and not currently_inside_any:
        character.shortcut_zone_entered_at = None


async def _check_teleport_by_location(character, old_location, new_location, ctx):
    """Detect teleportation via location delta and apply penalty.

    Hotfix for when the mod server hooks (ServerTeleportCharacter etc.) are
    unavailable. If a player who has recent Money deliveries moves more than
    TELEPORT_DISTANCE_THRESHOLD between ticks, trigger the same penalty
    as handle_teleport_or_respawn.
    """
    if not LOCATION_TELEPORT_DETECTION_ENABLED:
        return

    distance = old_location.distance(new_location)
    if distance <= TELEPORT_DISTANCE_THRESHOLD:
        return

    # Quick check: any recent Money deliveries?
    window_start = timezone.now() - timedelta(minutes=TELEPORT_DETECTION_WINDOW)
    has_recent_money = await Delivery.objects.filter(
        character=character,
        cargo_key="Money",
        timestamp__gte=window_start,
    ).aexists()
    if not has_recent_money:
        return

    # Skip police officers
    is_police = await PoliceSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aexists()
    if is_police:
        return

    logger.info(
        "Teleport detected via location delta for %s (distance=%.0f)",
        character.name, distance,
    )

    # Reuse the existing penalty handler from webhook.py
    from amc.webhook import handle_teleport_or_respawn

    http_client_mod = ctx.get("http_client_mod")
    http_client = ctx.get("http_client")
    # Synthesize a minimal event dict (the handler doesn't use event data)
    event = {"data": {}}
    await handle_teleport_or_respawn(
        event, character, timezone.now(), http_client_mod, http_client,
    )


async def _check_pois_and_portals(character, old_location, new_location, ctx):
    """Check POI entries and portal triggers using the cached last_location."""
    player = character.player
    http_client_mod = ctx.get("http_client_mod")
    if http_client_mod is None:
        return

    for target_point, target_radius_meters, message in point_of_interests:
        distance_to_new = new_location.distance(target_point)
        distance_to_old = old_location.distance(target_point)

        was_outside = distance_to_old > target_radius_meters
        is_inside = distance_to_new <= target_radius_meters

        if was_outside and is_inside:
            await show_popup(http_client_mod, message, player_id=player.unique_id)
            await asyncio.sleep(0.1)

    for source_point, source_radius_meters, target_point in portals:
        distance_to_new = new_location.distance(source_point)
        distance_to_old = old_location.distance(source_point)

        was_outside = distance_to_old > source_radius_meters
        is_inside = distance_to_new <= source_radius_meters

        if was_outside and is_inside:
            await teleport_player(
                http_client_mod,
                str(player.unique_id),
                {"X": target_point.x, "Y": target_point.y, "Z": target_point.z},
            )
            await asyncio.sleep(0.1)


@skip_if_running
async def monitor_locations(ctx):
    http_client = ctx.get("http_client_mod")
    async with http_client.get("/players") as resp:
        players = (await resp.json()).get("data", [])

    if not players:
        return

    # Build lookup: CharacterGuid -> player_info
    guid_to_player_info = {
        p["CharacterGuid"]: p for p in players if p.get("CharacterGuid")
    }

    if not guid_to_player_info:
        return

    # Single batch query: fetch all matching characters
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

        location_data = {
            axis.lower(): value for axis, value in player_info["Location"].items()
        }
        new_point = Point(**location_data)
        vehicle_key = player_info["VehicleKey"]

        # Use cached last_location instead of querying 175M-row table
        if character.last_location:
            await _check_pois_and_portals(
                character, character.last_location, new_point, ctx
            )
            await _check_shortcut_zones(
                character, character.last_location, new_point, ctx
            )
            await _check_teleport_by_location(
                character, character.last_location, new_point, ctx
            )

        # Queue for bulk operations
        new_locations.append(
            CharacterLocation(
                character=character,
                location=new_point,
                vehicle_key=vehicle_key,
            )
        )
        character.last_location = new_point
        character.last_vehicle_key = vehicle_key
        character.last_online = now
        characters_to_update.append(character)

    # Bulk insert all locations in one query (ignore timestamp conflicts)
    if new_locations:
        await CharacterLocation.objects.abulk_create(
            new_locations, ignore_conflicts=True
        )

    # Bulk update cached locations on Character
    if characters_to_update:
        await Character.objects.abulk_update(
            characters_to_update,
            ["last_location", "last_vehicle_key", "last_online", "shortcut_zone_entered_at"],
        )

