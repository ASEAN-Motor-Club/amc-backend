import asyncio
import logging
import math
from datetime import timedelta
from django.utils import timezone
from amc.command_framework import registry, CommandContext
from amc.heightmap import terrain_z_cm
from amc.models import TeleportPoint, RescueRequest, PoliceSession, Wanted
from amc.mod_server import (
    get_player,
    teleport_player,
    get_player_last_vehicle,
    show_popup,
    enter_last_vehicle,
)
from amc.game_server import get_players
from amc.police import is_police_vehicle
from django.conf import settings
from django.db.models import Q
from django.utils.translation import gettext as _, gettext_lazy

logger = logging.getLogger("amc.commands.teleport")


async def _auto_arrest_wanted_criminal(wanted, character, player, http_client_mod, reason: str = ""):
    """Run the full arrest flow when a wanted criminal attempts to teleport.

    Calls execute_arrest with officer_character=None (system arrest):
      - Expires the Wanted record.
      - Confiscates the criminal's bounty + delivery earnings.
      - Records confiscation to the treasury (no officer reward).
      - Teleports the criminal to jail.
      - Sets character.jailed_until for boundary enforcement.
      - Shows a popup with the arrest reason.
    """
    from amc.commands.faction import execute_arrest

    # Build the minimal synthetic targets / target_chars dicts execute_arrest expects.
    # Location is the character's last known position or a zero-vector sentinel.
    loc = character.last_location
    if loc is not None:
        crim_loc = (loc.x, loc.y, loc.z)
    else:
        crim_loc = (0.0, 0.0, 0.0)

    guid = character.guid or str(character.pk)
    targets = {guid: (str(player.unique_id), crim_loc, False)}
    target_chars = {guid: character}

    try:
        await execute_arrest(
            officer_character=None,
            targets=targets,
            target_chars=target_chars,
            http_client=None,
            http_client_mod=http_client_mod,
            reason=reason,
        )
    except ValueError as exc:
        # Jail TeleportPoint not configured — log and bail
        logger.warning("auto_arrest_wanted_criminal: %s", exc)
    except Exception:
        logger.exception(
            "auto_arrest_wanted_criminal: unexpected error for %s", character.name
        )


POLICE_TP_NEAR_WANTED_MESSAGE = (
    "<Title>Teleport Blocked</>\n"
    "<Warning>Destination is too close to a wanted suspect!</>\n"
    "Police cannot teleport within range of wanted criminals."
)


async def _check_police_tp_near_wanted(ctx: CommandContext, location: dict) -> bool:
    """Check if an on-duty police officer is teleporting near a wanted suspect.

    Returns True if the teleport should be blocked (officer near wanted suspect).
    """
    is_on_duty = await PoliceSession.objects.filter(
        character=ctx.character, ended_at__isnull=True
    ).aexists()
    if not is_on_duty or not ctx.http_client:
        return False

    from amc.commands.faction import _build_player_locations
    from amc.commands.police import SETWANTED_MIN_DISTANCE

    players_list = await get_players(ctx.http_client)
    if not players_list:
        return False

    locations = _build_player_locations(players_list)

    dest = (location["X"], location["Y"], location["Z"])

    async for wanted in Wanted.objects.filter(
        expired_at__isnull=True, wanted_remaining__gt=0
    ).select_related("character"):
        guid = wanted.character.guid
        if not guid or guid == str(ctx.character.guid):
            continue
        entry = locations.get(guid)
        if not entry:
            continue
        _name, suspect_loc, _vehicle = entry
        if _distance_3d(dest, suspect_loc) < SETWANTED_MIN_DISTANCE:
            asyncio.create_task(
                show_popup(
                    ctx.http_client_mod,
                    _(POLICE_TP_NEAR_WANTED_MESSAGE),
                    character_guid=ctx.character.guid,
                    player_id=str(ctx.player.unique_id),
                )
            )
            return True

    return False


def _distance_3d(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


@registry.register(
    ["/teleport vehicle", "/tp vehicle"],
    description=gettext_lazy(
        "Teleport to and enter your last used vehicle (Police Only)"
    ),
    category="Teleportation",
)
async def cmd_tp_vehicle(ctx: CommandContext):
    is_on_duty = await PoliceSession.objects.filter(
        character=ctx.character, ended_at__isnull=True
    ).aexists()

    if not is_on_duty:
        await ctx.reply(_("Police Only"))
        return

    if settings.TP_VEHICLE_USE_TELEPORT_FALLBACK:
        # Temporary fallback: find police vehicle via last vehicle endpoint
        try:
            last_vehicle = await get_player_last_vehicle(
                ctx.http_client_mod, str(ctx.character.guid)
            )
        except Exception:
            await ctx.reply(_("Could not fetch vehicles"))
            return

        vehicle = last_vehicle.get("vehicle")
        if not vehicle:
            await ctx.reply(_("No vehicles found"))
            return

        vehicle_name = vehicle.get("fullName", "").split(" ")[0].replace("_C", "")
        if not is_police_vehicle(vehicle_name):
            await ctx.reply(_("No police vehicle found"))
            return

        position = vehicle.get("position")
        if not position:
            await ctx.reply(_("Could not determine vehicle location"))
            return

        location = {"X": position["X"], "Y": position["Y"], "Z": position["Z"] + 100}
        await teleport_player(
            ctx.http_client_mod,
            ctx.player.unique_id,
            location,
            no_vehicles=True,
        )
    else:
        response = await enter_last_vehicle(ctx.http_client_mod, ctx.character.guid)
        if "error" in response:
            await ctx.reply(_("Could not enter vehicle: ") + response["error"])


@registry.register(
    ["/teleport", "/tp"],
    description=gettext_lazy("Teleport to coordinates (Admin Only)"),
    category="Teleportation",
)
async def cmd_tp_coords(ctx: CommandContext, x: int, y: int, z: int):
    if not (ctx.player_info and ctx.player_info.get("bIsAdmin")):
        await ctx.reply(_("Admin Only"))
        return

    await teleport_player(
        ctx.http_client_mod,
        ctx.player.unique_id,
        {"X": x, "Y": y, "Z": z},
        no_vehicles=False,
    )


@registry.register(
    ["/teleport", "/tp"],
    description=gettext_lazy("Teleport to a location"),
    category="Teleportation",
    featured=True,
)
async def cmd_tp_name(ctx: CommandContext, name: str = ""):
    CORPS_WITH_TP = {"69FF57844F3F79D1F9665991B4006325"}
    player_info = ctx.player_info or {}

    tp_points = TeleportPoint.objects.filter(character__isnull=True).order_by("name")
    tp_points_names = [tp.name async for tp in tp_points]

    current_vehicle = None
    try:
        last_vehicle = await get_player_last_vehicle(
            ctx.http_client_mod, str(ctx.character.guid)
        )
        current_vehicle = last_vehicle.get("vehicle")
    except Exception:
        pass

    no_vehicles = not player_info.get("bIsAdmin")

    # Police on duty: always no_vehicles, even for admins
    is_on_duty = await PoliceSession.objects.filter(
        character=ctx.character, ended_at__isnull=True
    ).aexists()
    if is_on_duty:
        no_vehicles = True

    location = None
    rescue_tp_data = None

    if name:
        try:
            teleport_point = await TeleportPoint.objects.aget(
                Q(character=ctx.character) | Q(character__isnull=True),
                name__iexact=name,
            )

            # Block police on duty from using restricted locations
            if teleport_point.name.lower() in ("dasa", "harbor"):
                is_police = await PoliceSession.objects.filter(
                    character=ctx.character, ended_at__isnull=True
                ).aexists()
                if is_police:
                    asyncio.create_task(
                        show_popup(
                            ctx.http_client_mod,
                            _(
                                "This teleport location is restricted while on police duty."
                            ),
                            character_guid=ctx.character.guid,
                            player_id=str(ctx.player.unique_id),
                        )
                    )
                    return

            loc_obj = teleport_point.location
            location = {"X": loc_obj.x, "Y": loc_obj.y, "Z": loc_obj.z}
        except TeleportPoint.DoesNotExist:
            asyncio.create_task(
                show_popup(
                    ctx.http_client_mod,
                    _(
                        "Teleport point not found\nChoose from one of the following locations:\n\n{locations}"
                    ).format(locations="\n".join(tp_points_names)),
                    character_guid=ctx.character.guid,
                    player_id=str(ctx.player.unique_id),
                )
            )
            return
    else:
        # Check for Rescue Responder permission
        recent_rescues = (
            RescueRequest.objects.filter(
                responders=ctx.player,
                timestamp__gte=timezone.now() - timedelta(minutes=10),
            )
            .select_related("character")
            .order_by("-timestamp")
        )

        async for rescue in recent_rescues:
            if rescue.location:
                rescue_tp_data = {
                    "requester_name": rescue.character.name,
                    "location": {
                        "X": rescue.location.x,
                        "Y": rescue.location.y,
                        "Z": rescue.location.z,
                    },
                }
                break

        if (
            player_info.get("bIsAdmin")
            or (current_vehicle and current_vehicle.get("companyGuid") in CORPS_WITH_TP)
            or rescue_tp_data
        ):
            # Block police on duty from custom destination teleport
            if is_on_duty and not rescue_tp_data:
                asyncio.create_task(
                    show_popup(
                        ctx.http_client_mod,
                        _(
                            "Custom destination teleport is restricted while on police duty."
                        ),
                        character_guid=ctx.character.guid,
                        player_id=str(ctx.player.unique_id),
                    )
                )
                return

            # Admins typing bare /tp need CustomDestinationAbsoluteLocation,
            # which the native game API doesn't expose — fetch from mod server.
            if (
                not name
                and not player_info.get("CustomDestinationAbsoluteLocation")
                and ctx.http_client_mod
                and ctx.player
            ):
                try:
                    mod_player_info = await get_player(
                        ctx.http_client_mod, str(ctx.player.unique_id)
                    )
                    if mod_player_info and mod_player_info.get(
                        "CustomDestinationAbsoluteLocation"
                    ):
                        player_info = {**player_info, **mod_player_info}
                except Exception:
                    pass

            # Teleport to Custom Waypoint
            no_vehicles = (
                not player_info.get("bIsAdmin") and not rescue_tp_data
            ) or is_on_duty
            location = player_info.get("CustomDestinationAbsoluteLocation")

            if location and rescue_tp_data and not player_info.get("bIsAdmin"):
                # Enforce distance limit for rescue responders
                origin = rescue_tp_data["location"]
                dx = location["X"] - origin["X"]
                dy = location["Y"] - origin["Y"]
                distance = math.sqrt(dx * dx + dy * dy)

                if distance > 10_000:
                    asyncio.create_task(
                        show_popup(
                            ctx.http_client_mod,
                            _(
                                "<Title>Rescue Teleport Restricted</>\n"
                                "Destination is {distance:.0f} units from {requester}.\n"
                                "Maximum allowed distance: <Highlight>10,000 units</>.\n\n"
                                "Move your custom destination marker closer to the requester."
                            ).format(
                                distance=distance,
                                requester=rescue_tp_data["requester_name"],
                            ),
                            character_guid=ctx.character.guid,
                            player_id=str(ctx.player.unique_id),
                        )
                    )
                    return

            if location:
                # The map waypoint's Z is unreliable — players clip through
                # the ground when it's used as-is.  Correct it to at least
                # the terrain height from the heightmap, but never *below*
                # the game-provided Z: the heightmap is bare terrain and
                # doesn't include buildings, piers, etc. that the
                # destination may sit on top of.
                terrain_z = terrain_z_cm(location["X"], location["Y"])
                base_z = location["Z"]
                if terrain_z is not None:
                    base_z = max(base_z, terrain_z)
                # Fix Z offset based on vehicle
                if player_info.get("VehicleKey") == "None":
                    location["Z"] = base_z + 100
                else:
                    location["Z"] = base_z + 5

    if not location:
        asyncio.create_task(
            show_popup(
                ctx.http_client_mod,
                _(
                    "<Title>Teleport</>\nUsage: <Highlight>/tp [location]</>\nChoose from one of the following locations:\n\n{locations}"
                ).format(locations="\n".join(tp_points_names)),
                character_guid=ctx.character.guid,
                player_id=str(ctx.player.unique_id),
            )
        )
        return

    if await _check_police_tp_near_wanted(ctx, location):
        return

    await teleport_player(
        ctx.http_client_mod,
        ctx.player.unique_id,
        location,
        no_vehicles=no_vehicles,
        reset_trailers=not player_info.get("bIsAdmin"),
        reset_carried_vehicles=not player_info.get("bIsAdmin"),
    )
