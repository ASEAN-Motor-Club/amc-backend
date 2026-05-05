from typing import Optional
from amc.command_framework import registry, CommandContext
import asyncio
import itertools
from amc.mod_server import get_player_last_vehicle, get_player_last_vehicle_parts, despawn_by_tag
from amc.game_server import get_players
from amc.vehicles import (
    format_vehicle_name,
    format_vehicle_part_game,
    despawn_personal_vehicles,
    register_player_vehicles,
    spawn_registered_vehicle,
    format_key_string,
)
from amc.mod_detection import (
    detect_custom_parts,
    detect_incompatible_parts,
    format_custom_parts_game,
    format_incompatible_parts_game,
    POLICE_DUTY_WHITELIST,
)
from amc.models import CharacterVehicle, PoliceSession
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


@registry.register(
    "/unrental",
    description=gettext_lazy("Stop renting out your vehicle"),
    category="Vehicle Management",
)
async def cmd_unrental(ctx: CommandContext, category: str = ""):
    category = category.strip()
    if category == "all":
        vehicles = [v async for v in CharacterVehicle.objects.filter(character=ctx.character, rental=True)]
    elif category.isdigit():
        vehicles = [v async for v in CharacterVehicle.objects.filter(character=ctx.character, pk=int(category))]
    else:
        vehicles = await register_player_vehicles(ctx.http_client_mod, ctx.character, ctx.player, active=True)

    if not vehicles:
        await ctx.reply(_("<Title>Removing rentals</>\nUsage: /unrental, /unrental 2345, /unrental all"))
        return

    for v in vehicles:
        if v.rental:
            await despawn_by_tag(ctx.http_client_mod, f"rental-{v.id}")
            v.rental = False
            await v.asave()
    await ctx.reply(_("Rentals removed"))


@registry.register(
    "/rental",
    description=gettext_lazy("Mark vehicle as for rental"),
    category="Vehicle Management",
)
async def cmd_rental(ctx: CommandContext, alias: str = ""):
    vehicles = await register_player_vehicles(ctx.http_client_mod, ctx.character, ctx.player, active=True)
    own_company_guid = ctx.player_info.get("OwnCompanyGuid") if ctx.player_info else None
    vehicles = (
        [v for v in vehicles if v.config.get("CompanyName") and v.company_guid == own_company_guid]
        if vehicles
        else []
    )

    if not vehicles:
        await ctx.reply(_("<Title>Rental System</>\nOnly Corporation vehicles can be rented out."))
        return

    for v in vehicles:
        if not v.rental:
            v.rental = True
        if alias.strip():
            v.alias = alias.strip()
        await v.asave()

    names = "\n".join(
        [f"<Small>#{v.id} - {v.config['VehicleName']}</>" for v in vehicles if v.rental]
    )
    await ctx.reply(_("<Title>Marked as rental</>\nPlayers can /rent these:\n\n{names}").format(names=names))


@registry.register(
    "/rent",
    description=gettext_lazy("Rent a vehicle"),
    category="Vehicle Management",
)
async def cmd_rent(ctx: CommandContext, vehicle_id: str = ""):
    if not vehicle_id or not vehicle_id.isdigit():
        vehicles = [v async for v in CharacterVehicle.objects.filter(rental=True)]
        if vehicle_id.strip():
            search = vehicle_id.strip().lower()
            vehicles = [
                v
                for v in vehicles
                if search in format_key_string(v.config["VehicleName"]).lower()
            ]

        if not vehicles:
            await ctx.reply(_("<Title>Rentals</>\nNo rentals found."))
            return

        vehicles.sort(key=lambda v: v.config.get("CompanyName", "Independent"))

        lines: list[str] = []
        for company, group in itertools.groupby(
            vehicles, key=lambda v: v.config.get("CompanyName", "Independent")
        ):
            lines.append(f"<Bold>{company}</>")
            for v in group:
                lines.append(f" <Small>#{v.id} - {v.config['VehicleName']}</>")
            lines.append("")

        names = "\n".join(lines)
        await ctx.reply(
            _("<Title>Available Rentals</>\nType /rent [id] to rent.\n\n{names}").format(names=names)
        )
    else:
        try:
            v = await CharacterVehicle.objects.aget(pk=vehicle_id, rental=True)
            if not ctx.player_info:
                await ctx.reply(_("Player info not found"))
                return
            loc = ctx.player_info["Location"]
            loc["Z"] -= 100
            await spawn_registered_vehicle(
                ctx.http_client_mod,
                v,
                loc,
                driver_guid=ctx.character.guid,
                tags=[ctx.character.name, "rental_vehicles", f"rental-{v.id}"],
            )
            await ctx.reply(
                _("Brought to you by {company}").format(company=v.config.get("CompanyName"))
            )
        except CharacterVehicle.DoesNotExist:
            await ctx.reply(_("Rental not found"))
