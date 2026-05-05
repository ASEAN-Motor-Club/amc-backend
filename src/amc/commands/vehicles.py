from typing import Optional
from amc.command_framework import registry, CommandContext
import asyncio
from amc.mod_server import get_player_last_vehicle, get_player_last_vehicle_parts
from amc.game_server import get_players
from amc.vehicles import format_vehicle_name, format_vehicle_part_game, despawn_personal_vehicles
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
)
async def cmd_despawn(ctx: CommandContext, category: str = "all"):
    from amc.mod_server import despawn_player_vehicle

    if category == "personal":
        await despawn_personal_vehicles(ctx.http_client_mod, ctx.character)
        return

    try:
        await despawn_player_vehicle(
            ctx.http_client_mod,
            str(ctx.character.guid),
            category=category,
        )
    except Exception:
        pass


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
        # Look up the target character GUID from the player data
        target_character_guid = None
        for pid, p_data in players:
            if pid == target_pid:
                target_character_guid = p_data.get("character_guid")
                break
    else:
        target_character_guid = str(ctx.character.guid)
        target_player_name = ctx.character.name

    # Fetch last vehicle and parts via new lightweight endpoints
    try:
        last_vehicle, parts_data = await asyncio.gather(
            get_player_last_vehicle(ctx.http_client_mod, target_character_guid),
            get_player_last_vehicle_parts(ctx.http_client_mod, target_character_guid, complete=False),
        )
    except Exception:
        await ctx.reply(
            _("<Title>Error</>\n\nFailed to fetch vehicle data. Is the player online?")
        )
        return

    vehicle = last_vehicle.get("vehicle")
    if not vehicle:
        await ctx.reply(
            _("<Title>No Vehicle</>\n\n{name} has no active vehicle.").format(
                name=target_player_name
            )
        )
        return

    vehicle_name = format_vehicle_name(vehicle["fullName"])
    parts = parts_data.get("parts", [])
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
            ctx.character,
            ctx.http_client_mod,
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
            _(
                "<Title>Mod Check</>\n\n<Bold>{name}</> — {vehicle}{drive}{issues}"
            ).format(
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


@registry.register(
    "/check_parts",
    description=gettext_lazy("List all parts on a player's vehicle"),
    category="Vehicle Management",
)
async def cmd_check_parts(ctx: CommandContext, target_player_name: Optional[str] = None):
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
        target_character_guid = None
        for pid, p_data in players:
            if pid == target_pid:
                target_character_guid = p_data.get("character_guid")
                break
    else:
        target_character_guid = str(ctx.character.guid)
        target_player_name = ctx.character.name

    try:
        last_vehicle, parts_data = await asyncio.gather(
            get_player_last_vehicle(ctx.http_client_mod, target_character_guid),
            get_player_last_vehicle_parts(ctx.http_client_mod, target_character_guid, complete=False),
        )
    except Exception:
        await ctx.reply(
            _("<Title>Error</>\n\nFailed to fetch vehicle data. Is the player online?")
        )
        return

    vehicle = last_vehicle.get("vehicle")
    if not vehicle:
        await ctx.reply(
            _("<Title>No Vehicle</>\n\n{name} has no active vehicle.").format(
                name=target_player_name
            )
        )
        return

    vehicle_name = format_vehicle_name(vehicle["fullName"])
    parts = parts_data.get("parts", [])

    if not parts:
        await ctx.reply(
            _(
                "<Title>Parts List</>"
                "\n\n<Bold>{name}</> — {vehicle}"
                "\n\nNo parts found."
            ).format(
                name=target_player_name,
                vehicle=vehicle_name,
            )
        )
        return

    sorted_parts = sorted(parts, key=lambda p: p.get("Slot", 0))
    parts_lines = "\n".join(format_vehicle_part_game(p) for p in sorted_parts)

    await ctx.reply(
        _(
            "<Title>Parts List</>"
            "\n\n<Bold>{name}</> — {vehicle}"
            "\n\n{parts}"
        ).format(
            name=target_player_name,
            vehicle=vehicle_name,
            parts=parts_lines,
        )
    )
