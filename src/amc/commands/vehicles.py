from typing import Optional
from amc.command_framework import registry, CommandContext
from amc.mod_server import list_player_vehicles
from amc.game_server import get_players
from amc.vehicles import format_vehicle_name
from amc.mod_detection import (
    detect_custom_parts,
    detect_incompatible_parts,
    format_custom_parts_game,
    format_incompatible_parts_game,
    POLICE_DUTY_WHITELIST,
)
from amc.models import PoliceSession
from amc.player_tags import refresh_player_name
from amc.utils import fuzzy_find_player
from django.utils.translation import gettext as _, gettext_lazy


@registry.register(
    ["/despawn", "/d"],
    description=gettext_lazy("Despawn your vehicle"),
    category="Vehicle Management",
    # deprecated=True,
    # deprecated_message="<Title>Command Deprecated</>\nThe /despawn command is no longer available.\nVehicles now despawn automatically."
)
async def cmd_despawn(ctx: CommandContext, category: str = "all"):
    from amc.mod_server import send_system_message

    await send_system_message(
        ctx.http_client_mod,
        _("Despawn is temporarily disabled."),
        character_guid=ctx.character.guid,
    )


@registry.register(
    "/check_mods",
    description=gettext_lazy("Check a player's vehicle for custom parts"),
    category="Vehicle Management",
)
async def cmd_check_mods(ctx: CommandContext, target_player_name: Optional[str] = None):
    # Resolve target player ID (only admins can check other players)
    is_admin = ctx.player_info and ctx.player_info.get("bIsAdmin")
    checking_self = not (target_player_name and is_admin)
    if not checking_self:
        players = await get_players(ctx.http_client)
        target_pid = fuzzy_find_player(players, target_player_name)
        if not target_pid:
            await ctx.reply(
                _(
                    "<Title>Player not found</>"
                    "\n\nCould not find a player matching that name."
                )
            )
            return
    else:
        target_pid = str(ctx.player.unique_id)
        target_player_name = ctx.character.name

    # Fetch only the active (main) vehicle
    try:
        player_vehicles = await list_player_vehicles(
            ctx.http_client_mod, target_pid, active=True, complete=True
        )
    except Exception:
        await ctx.reply(
            _("<Title>Error</>\n\nFailed to fetch vehicle data. Is the player online?")
        )
        return

    if not player_vehicles:
        await ctx.reply(
            _("<Title>No Vehicle</>\n\n{name} has no active vehicle.").format(
                name=target_player_name
            )
        )
        return

    # Check the first (main) vehicle
    vehicle = next(
        (
            v
            for v in player_vehicles.values()
            if v.get("isLastVehicle") and v.get("index", -1) == 0
        ),
        None,
    )

    if not vehicle:
        await ctx.reply(
            _(
                "<Title>No Main Vehicle</>"
                "\n\nCould not identify the main vehicle among active vehicles."
            )
        )
        return
    vehicle_name = format_vehicle_name(vehicle["fullName"])
    parts = vehicle.get("parts", [])
    # Whitelist police parts for officers on active duty
    whitelist = None
    is_on_duty = await PoliceSession.objects.filter(
        character=ctx.character, ended_at__isnull=True
    ).aexists()
    if is_on_duty:
        whitelist = POLICE_DUTY_WHITELIST
    custom = detect_custom_parts(parts, whitelist=whitelist)
    incompatible = detect_incompatible_parts(parts, vehicle["fullName"])

    # Recalculate [MODS] tag when checking own vehicle
    if checking_self:
        await refresh_player_name(
            ctx.character, ctx.http_client_mod,
            has_custom_parts=bool(custom or incompatible),
        )

    # Build drivetrain summary from DriveInfo
    drive_info = vehicle.get("DriveInfo", {})
    drive_line = ""
    if drive_info:
        drive_type = drive_info.get("drive_type", "Unknown")
        effective = drive_info.get("effective_drive_type", drive_type)
        driven = drive_info.get("driven_wheel_count", 0)
        total = drive_info.get("total_wheel_count", 0)
        axles = drive_info.get("total_axle_count", 0)
        driven_axles = len(drive_info.get("driven_axle_indices", []))

        label = drive_type
        if effective != drive_type:
            label = f"{drive_type} ({effective})"

        drive_line = f"\nDrivetrain: {label} — {driven}/{total} wheels, {driven_axles}/{axles} axles"

    issues = []
    if custom:
        issues.append(
            _("\n{count} custom part(s):\n\n{parts}").format(
                count=len(custom),
                parts=format_custom_parts_game(custom),
            )
        )
    if incompatible:
        issues.append(
            _("\n{count} incompatible part(s):\n\n{parts}").format(
                count=len(incompatible),
                parts=format_incompatible_parts_game(incompatible),
            )
        )

    if issues:
        await ctx.reply(
            _("<Title>Mod Check</>\n\n<Bold>{name}</> — {vehicle}{drive}{issues}").format(
                name=target_player_name,
                vehicle=vehicle_name,
                drive=drive_line,
                issues="\n".join(issues),
            )
        )
    else:
        await ctx.reply(
            _(
                "<Title>Parts Check</>"
                "\n\n<Bold>{name}</> — {vehicle}{drive}"
                "\n\nAll stock parts."
            ).format(
                name=target_player_name,
                vehicle=vehicle_name,
                drive=drive_line,
            )
        )
