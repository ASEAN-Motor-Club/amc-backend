import asyncio
import logging
from datetime import timedelta
from decimal import Decimal
from typing import Optional
from django.db.models import F
from amc.command_framework import registry, CommandContext
from amc.mod_server import (
    show_popup,
    despawn_by_tag,
    spawn_garage,
    get_player,
    spawn_assets,
    spawn_vehicle,
    spawn_dealership,
    force_exit_vehicle,
    get_players as get_players_mod,
    teleport_player,
    transfer_money,
    get_vehicle_cargos,
    set_world_vehicle_decal,
    mute_player,
    unmute_player,
)
from amc.game_server import get_players, add_player_role, remove_player_role
from amc.vehicles import spawn_registered_vehicle, register_player_vehicles
from amc.models import (
    Character,
    CharacterVehicle,
    Player,
    VehicleDealership,
    WorldText,
    WorldObject,
    Garage,
    TeleportPoint,
)
from amc.enums import VehicleKey, VehicleKeyByLabel
from django.contrib.gis.geos import Point
from django.utils import timezone
from django.utils.translation import gettext as _, gettext_lazy
from amc.utils import fuzzy_find_player
from amc.player_tags import strip_all_tags, refresh_player_name
from amc.forced_name import log_forced_name_change
from amc.mute import persist_mute, clear_persistent_mute
from amc_finance.services import player_donation

logger = logging.getLogger(__name__)


@registry.register(
    "/apply_world_vehicles",
    description=gettext_lazy("Apply decals/parts to world vehicles"),
    category="Admin",
)
async def cmd_apply_world_vehicles(ctx: CommandContext):
    if not ctx.player_info or not ctx.player_info.get("bIsAdmin"):
        return
    async for v in CharacterVehicle.objects.filter(is_world_vehicle=True):
        await set_world_vehicle_decal(
            ctx.http_client_mod,
            f"{v.config['VehicleName']}_C",
            customization=v.config["Customization"],
            decal=v.config["Decal"],
            parts=[{**p, "partKey": p["Key"]} for p in v.config["Parts"]],
        )
    await ctx.reply(_("World vehicle decals and parts applied."))


@registry.register(
    "/spawn_displays",
    description=gettext_lazy("Spawn display vehicles"),
    category="Admin",
)
async def cmd_spawn_displays(ctx: CommandContext, display_id: Optional[int] = None):
    if not ctx.player_info or not ctx.player_info.get("bIsAdmin"):
        return
    qs = CharacterVehicle.objects.select_related("character").filter(
        spawn_on_restart=True
    )
    if display_id:
        qs = qs.filter(pk=display_id)

    async for v in qs:
        tags = [f"display-{v.id}"]
        if v.character:
            tags.extend([v.character.name, f"display-{v.character.guid}"])
        await despawn_by_tag(ctx.http_client_mod, f"display-{v.id}")
        await spawn_registered_vehicle(
            ctx.http_client_mod,
            v,
            tag="display_vehicles",
            extra_data={
                "companyName": f"{v.character.name}'s Display",
                "drivable": v.rental,
            }
            if v.character
            else {},
            tags=tags,
        )


@registry.register(
    "/spawn_dealerships",
    description=gettext_lazy("Spawn dealership vehicles"),
    category="Admin",
)
async def cmd_spawn_dealerships(ctx: CommandContext):
    if ctx.player_info and ctx.player_info.get("bIsAdmin"):
        async for vd in VehicleDealership.objects.filter(spawn_on_restart=True):
            await vd.spawn(ctx.http_client_mod)


@registry.register(
    "/spawn_dealership",
    description=gettext_lazy("Spawn a vehicle dealership at your position (Admin)"),
    category="Admin",
)
async def cmd_spawn_dealership(ctx: CommandContext, vehicle_label: Optional[str] = None):
    if not ctx.player_info or not ctx.player_info.get("bIsAdmin"):
        await ctx.reply(_("Admin-only"))
        return

    if vehicle_label is None:
        await ctx.reply(
            _("<Title>Spawn Dealership</>\n\n") + "\n".join(VehicleKey.labels)
        )
        return

    vehicle_key = VehicleKeyByLabel.get(vehicle_label)
    if vehicle_key is None:
        await ctx.reply(
            _("<Title>Unknown vehicle</>\n\nUse /spawn to list vehicle labels.")
        )
        return

    # 0) Read the admin's facing (pawn Yaw) from the mod endpoint so the pad
    #    faces the same way the admin is looking (freeman 2026-09-02). The
    #    chat-context player_info (native API) carries Location but no
    #    Rotation, so this needs a fresh mod fetch. Falls back to 0.0 (the
    #    previous behaviour) if the fetch fails or has no Rotation.
    try:
        player_data = await get_player(
            ctx.http_client_mod, str(ctx.player.unique_id), force_refresh=True
        )
    except Exception:
        player_data = None
    rot = player_data.get("Rotation", {}) if player_data else {}
    yaw = rot.get("Yaw", 0.0)

    loc = ctx.player_info.get("Location")
    if not loc:
        await ctx.reply(_("<Title>No location</>\n\nCould not read your position."))
        return

    x, y, z = loc["X"], loc["Y"], loc["Z"]

    # 1) Teleport the admin straight up first. Verified live 2026-09-01: a
    #    player standing near the pad origin blocks the dealership spawn.
    try:
        await teleport_player(
            ctx.http_client_mod,
            str(ctx.player.unique_id),
            {"X": x, "Y": y, "Z": z + 1000},
        )
    except Exception:
        await ctx.reply(_("<Title>Teleport failed</>\n\nDealership not placed."))
        return

    # 2) Spawn IMMEDIATELY after the teleport — NO sleep here: gravity pulls
    #    the teleported player back down and a delay lets them re-enter the
    #    block radius (freeman 2026-09-01). The mod's _write_limiter serializes
    #    the two calls.
    # 3) Dealer plot Z = playerZ - 90 (freeman 2026-09-01: the game reports a
    #    player's position 100 above ground, but the pad origin sits best at
    #    -90; calibrated against working dealer pads).
    pad_z = z - 90
    await spawn_dealership(
        ctx.http_client_mod,
        vehicle_key,
        {"X": x, "Y": y, "Z": pad_z},
        yaw,
    )

    # 4) Persist so the plot respawns on server restart.
    dealership = await VehicleDealership.objects.acreate(
        vehicle_key=vehicle_key,
        location=Point(x, y, pad_z),
        yaw=yaw,
        spawn_on_restart=True,
        notes=f"/spawn_dealership by {ctx.character.name}",
    )
    await ctx.reply(
        _(
            "<Title>Dealership placed</>\n\n{vehicle} dealer at X={x:.0f} Y={y:.0f} (row {id}). Respawns on restart."
        ).format(vehicle=vehicle_label, x=x, y=y, id=dealership.id)
    )


@registry.register(
    "/spawn_assets", description=gettext_lazy("Spawn world assets"), category="Admin"
)
async def cmd_spawn_assets(ctx: CommandContext):
    if ctx.player_info and ctx.player_info.get("bIsAdmin"):
        async for wt in WorldText.objects.all():
            await spawn_assets(ctx.http_client_mod, wt.generate_asset_data())
        async for wo in WorldObject.objects.all():
            await spawn_assets(ctx.http_client_mod, [wo.generate_asset_data()])


@registry.register(
    "/spawn_garages", description=gettext_lazy("Spawn garages"), category="Admin"
)
async def cmd_spawn_garages(ctx: CommandContext):
    if ctx.player_info and ctx.player_info.get("bIsAdmin"):
        async for g in Garage.objects.filter(spawn_on_restart=True):
            if g.config is None:
                continue
            # Despawn existing garage before spawning to avoid duplicates
            if g.tag:
                try:
                    await despawn_by_tag(ctx.http_client_mod, g.tag)
                except Exception:
                    pass
            resp = await spawn_garage(
                ctx.http_client_mod, g.config["Location"], g.config["Rotation"]
            )
            g.tag = resp.get("tag")
            await g.asave()


@registry.register(
    "/spawn_garage", description=gettext_lazy("Spawn a single garage"), category="Admin"
)
async def cmd_spawn_garage_single(ctx: CommandContext, name: str):
    if ctx.player_info and ctx.player_info.get("bIsAdmin"):
        loc = {**ctx.player_info["Location"]}
        loc["Z"] -= 100
        player_data = await get_player(
            ctx.http_client_mod, str(ctx.player.unique_id), force_refresh=True
        )
        rot = player_data.get("Rotation", {}) if player_data else {}
        resp = await spawn_garage(ctx.http_client_mod, loc, rot)
        tag = resp.get("tag")
        await Garage.objects.acreate(
            config={"Location": loc, "Rotation": rot},
            notes=name.strip(),
            tag=tag,
            hostname="asean-mt-server",
        )


@registry.register(
    "/remove_garage",
    description=gettext_lazy("Remove nearby garages (within 10m)"),
    category="Admin",
)
async def cmd_remove_garage(ctx: CommandContext):
    if not ctx.player_info or not ctx.player_info.get("bIsAdmin"):
        return

    player_loc = ctx.player_info["Location"]
    player_x, player_y, player_z = player_loc["X"], player_loc["Y"], player_loc["Z"]

    RADIUS = 1000

    removed_count = 0
    failed_despawn_count = 0

    async for garage in Garage.objects.all():
        if garage.config is None:
            continue
        garage_loc = garage.config.get("Location")
        if not garage_loc:
            continue

        gx, gy, gz = garage_loc["X"], garage_loc["Y"], garage_loc["Z"]
        distance = (
            (player_x - gx) ** 2 + (player_y - gy) ** 2 + (player_z - gz) ** 2
        ) ** 0.5
        if distance > RADIUS:
            continue

        if garage.tag:
            try:
                await despawn_by_tag(ctx.http_client_mod, garage.tag)
            except Exception:
                failed_despawn_count += 1

        await garage.adelete()
        removed_count += 1

    if removed_count > 0:
        msg = _(
            "<Title>Garage Removed</>\n\nRemoved {count} garage(s) near your location."
        ).format(count=removed_count)
        if failed_despawn_count > 0:
            msg += _(
                "\n\n<Warning>Failed to despawn {count} garage(s) from the game world. They were removed from the database only.</>"
            ).format(count=failed_despawn_count)
        await ctx.reply(msg)
    else:
        await ctx.reply(
            _(
                "<Title>No Garages Found</>\n\nNo garages within 10m of your location."
            )
        )


@registry.register(
    "/spawn", description=gettext_lazy("Spawn a vehicle"), category="Admin"
)
async def cmd_spawn(ctx: CommandContext, vehicle_label: Optional[str] = None):
    if not ctx.player_info or not ctx.player_info.get("bIsAdmin"):
        await ctx.reply(_("Admin-only"))
        return

    if not vehicle_label:
        await ctx.reply(_("<Title>Spawn Vehicle</>\n\n") + "\n".join(VehicleKey.labels))
    elif vehicle_label.isdigit():
        vehicle = await CharacterVehicle.objects.aget(pk=int(vehicle_label))
        loc = ctx.player_info["Location"]
        loc["Z"] -= 5
        await spawn_registered_vehicle(
            ctx.http_client_mod,
            vehicle,
            loc,
            driver_guid=ctx.character.guid,
            tags=["spawned_vehicles"],
        )
    else:
        await spawn_vehicle(
            ctx.http_client_mod,
            vehicle_label,
            ctx.player_info["Location"],
            driver_guid=ctx.character.guid,
        )


@registry.register(
    "/exit", description=gettext_lazy("Force exit vehicle (Admin)"), category="Admin"
)
async def cmd_exit(ctx: CommandContext, target_player_name: str):
    if ctx.player_info and ctx.player_info.get("bIsAdmin"):
        players = await get_players_mod(ctx.http_client_mod)
        if players is None:
            return
        target_guid = next(
            (
                p["CharacterGuid"]
                for p in players
                if p["PlayerName"] == target_player_name
                or strip_all_tags(p["PlayerName"]) == target_player_name
            ),
            None,
        )
        if target_guid:
            await force_exit_vehicle(ctx.http_client_mod, target_guid)


@registry.register(
    "/tp_player",
    description=gettext_lazy("Teleport a player to a location (Admin)"),
    category="Admin",
)
async def cmd_tp_player(
    ctx: CommandContext, target_player_name: str, location_name: str
):
    if not ctx.player_info or not ctx.player_info.get("bIsAdmin"):
        await ctx.reply(_("Admin-only"))
        return

    # Find the target player
    players = await get_players(ctx.http_client)
    target_pid = fuzzy_find_player(players, target_player_name)

    if not target_pid:
        asyncio.create_task(
            show_popup(
                ctx.http_client_mod,
                _(
                    "<Title>Player not found</>\n\nPlease make sure you typed the name correctly."
                ),
                character_guid=ctx.character.guid,
                player_id=str(ctx.player.unique_id),
            )
        )
        return

    if str(target_pid) == str(ctx.player.unique_id):
        await ctx.reply(
            _("You cannot teleport yourself with this command. Use /tp instead.")
        )
        return

    # Find the location
    try:
        teleport_point = await TeleportPoint.objects.aget(name__iexact=location_name)
        loc_obj = teleport_point.location
        location = {"X": loc_obj.x, "Y": loc_obj.y, "Z": loc_obj.z}
    except TeleportPoint.DoesNotExist:
        tp_points = TeleportPoint.objects.filter(character__isnull=True).order_by(
            "name"
        )
        tp_points_names = [tp.name async for tp in tp_points]
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

    # Teleport
    is_jail = teleport_point.name.lower() == "jail"

    if is_jail:
        try:
            await force_exit_vehicle(ctx.http_client_mod, str(target_pid))
            await asyncio.sleep(1.5)
        except Exception:
            pass

    await teleport_player(
        ctx.http_client_mod,
        str(target_pid),
        location,
        no_vehicles=is_jail,
        force=is_jail,
        reset_trailers=False,
        reset_carried_vehicles=False,
    )

    await show_popup(
        ctx.http_client_mod,
        _(
            "<Title>Teleported</>\n\nYou have been teleported to {location} by {admin}."
        ).format(location=location_name, admin=ctx.character.name),
        player_id=str(target_pid),
    )

    if is_jail:
        # Apply jail boundary enforcement for 60 seconds
        target_player_data = next(
            (p for pid, p in players if str(pid) == str(target_pid)), None
        )
        if target_player_data:
            try:
                target_character = await Character.objects.aget(
                    guid=target_player_data["character_guid"]
                )
                target_character.jailed_until = timezone.now() + timedelta(seconds=60)
                await target_character.asave(
                    update_fields=["jailed_until"]
                )
            except Character.DoesNotExist:
                pass

        await show_popup(
            ctx.http_client_mod,
            _(
                "<Title>Arrested</>\n<Warning>You have been jailed by an admin.</>\n"
                "You will be released in 60 seconds."
            ),
            player_id=str(target_pid),
        )

    await ctx.reply(
        _("Teleported {player} to {location}").format(
            player=target_player_name, location=location_name
        )
    )


BILL_AMOUNT = 50_000
BILL_MAX_LEVEL = 400


@registry.register(
    "/bill",
    description=gettext_lazy("Bill a player (Admin)"),
    category="Admin",
    deprecated=True,
)
async def cmd_bill(ctx: CommandContext, target_player_name: str):
    if not ctx.player_info or not ctx.player_info.get("bIsAdmin"):
        return

    # Find the target player
    players = await get_players(ctx.http_client)
    target_pid = fuzzy_find_player(players, target_player_name)

    if not target_pid:
        asyncio.create_task(
            show_popup(
                ctx.http_client_mod,
                _(
                    "<Title>Player not found</>\n\nPlease make sure you typed the name correctly."
                ),
                character_guid=ctx.character.guid,
                player_id=str(ctx.player.unique_id),
            )
        )
        return

    # Look up the target character
    target_player_data = next(
        (p for pid, p in players if str(pid) == str(target_pid)), None
    )
    if not target_player_data:
        return

    try:
        target_character = await Character.objects.aget(
            guid=target_player_data["character_guid"]
        )
    except Character.DoesNotExist:
        await ctx.reply(_("Character not found in database."))
        return

    if not target_character.driver_level:
        await ctx.reply(
            _("Cannot bill {name}: no driver level.").format(name=target_character.name)
        )
        return

    # Scale amount by driver level (same formula as UBI)
    amount = int(
        min(
            Decimal(str(BILL_AMOUNT)),
            Decimal(str(target_character.driver_level))
            * Decimal(str(BILL_AMOUNT))
            / BILL_MAX_LEVEL,
        )
    )

    if amount <= 0:
        return

    # Deduct from player wallet
    await transfer_money(
        ctx.http_client_mod, -amount, "Public service bill", str(target_pid)
    )

    # Record as donation to treasury
    await player_donation(amount, target_character, description="Public service bill")

    # Record gov worker contribution
    target_character.gov_employee_contributions = (
        F("gov_employee_contributions") + amount
    )
    await target_character.asave(update_fields=["gov_employee_contributions"])

    await ctx.reply(
        _("Billed {name} for {amount:,} coins.").format(
            name=target_character.name, amount=amount
        )
    )
    await ctx.announce(
        f"{target_character.name} has been billed {amount:,} for public service."
    )


@registry.register(
    "/cargo",
    description=gettext_lazy("Check cargo in a player's current vehicle (Admin)"),
    category="Admin",
)
async def cmd_cargo(ctx: CommandContext, target_player_name: Optional[str] = None):
    if not ctx.player_info or not ctx.player_info.get("bIsAdmin"):
        await ctx.reply(_("Admin-only"))
        return

    # Resolve target character GUID
    if target_player_name:
        players = await get_players_mod(ctx.http_client_mod)
        if players is None:
            await ctx.reply(_("Could not fetch player list."))
            return
        target = next(
            (
                p
                for p in players
                if p.get("PlayerName") == target_player_name
                or strip_all_tags(p.get("PlayerName", "")) == target_player_name
            ),
            None,
        )
        if not target:
            await ctx.reply(
                _("Player '{name}' not found.").format(name=target_player_name)
            )
            return
        character_guid = target.get("CharacterGuid")
        display_name = target.get("PlayerName", target_player_name)
    else:
        character_guid = str(ctx.character.guid)
        display_name = ctx.character.name

    if not character_guid:
        await ctx.reply(_("Could not resolve character GUID."))
        return

    vehicles = await get_vehicle_cargos(ctx.http_client_mod, character_guid)

    if vehicles is None:
        await ctx.reply(
            _("<Title>No Vehicle</>\n\n{name} is not in a vehicle.").format(
                name=display_name
            )
        )
        return

    # Build a readable summary
    lines = [_("<Title>Vehicle Cargo — {name}</>").format(name=display_name)]
    total_items = 0

    for v_idx, vehicle in enumerate(vehicles):
        vehicle_name = vehicle.get("fullName", f"Vehicle {v_idx + 1}").split(" ")[0].replace("_C", "")
        cargo_spaces = vehicle.get("cargoSpaces", [])

        cargo_lines = []
        for space in cargo_spaces:
            cargos = space.get("cargos", [])
            for c in cargos:
                total_items += 1
                key = c.get("Net_CargoKey", "Unknown")
                weight = c.get("Net_Weight", 0)
                delivery_id = c.get("Net_DeliveryId", 0)
                damage = c.get("Net_Damage", 0)
                payment = c.get("Net_Payment") or {}
                pay_amount = payment.get("ShadowedValue") or payment.get("BaseValue", 0)
                is_empty = c.get("Net_bIsEmptyContainer", False)

                parts = [f"{key}"]
                if weight:
                    parts.append(f"{weight:.0f}kg")
                if delivery_id:
                    parts.append(_("Delivery #{id}").format(id=delivery_id))
                if pay_amount:
                    parts.append(f"${pay_amount:,}")
                if damage > 0:
                    parts.append(_("dmg:{d:.0f}%").format(d=damage * 100))
                if is_empty:
                    parts.append(_("(empty container)"))

                cargo_lines.append("  • " + " | ".join(parts))

        if cargo_lines:
            lines.append(f"\n[{vehicle_name}]")
            lines.extend(cargo_lines)
        else:
            lines.append(f"\n[{vehicle_name}] — " + _("empty"))

    if total_items == 0:
        lines.append(_("\nNo cargo loaded."))

    await ctx.reply("\n".join(lines))


@registry.register(
    "/mute",
    description=gettext_lazy("Mute a player (Admin)"),
    category="Admin",
)
async def cmd_mute(ctx: CommandContext, target_player_name: str, duration: Optional[str] = None):
    if not ctx.player_info or not ctx.player_info.get("bIsAdmin"):
        await ctx.reply(_("Admin-only"))
        return

    players = await get_players_mod(ctx.http_client_mod)
    if players is None:
        await ctx.reply(_("Could not fetch player list."))
        return
    target = next(
        (
            p
            for p in players
            if p.get("PlayerName") == target_player_name
            or strip_all_tags(p.get("PlayerName", "")) == target_player_name
        ),
        None,
    )
    if not target:
        await ctx.reply(_("Player '{name}' not found.").format(name=target_player_name))
        return

    target_unique_id = target.get("UniqueID")
    display_name = target.get("PlayerName", target_player_name)

    if not target_unique_id:
        await ctx.reply(_("Could not resolve player ID."))
        return

    if duration is None:
        mute_for = True
    elif duration.isdigit():
        mute_for = int(duration)
    else:
        await ctx.reply(_("Invalid duration. Use a number of seconds or omit for permanent."))
        return

    try:
        await mute_player(ctx.http_client_mod, target_unique_id, mute_for=mute_for)
    except Exception as e:
        await ctx.reply(_("Failed to mute player: {error}").format(error=str(e)))
        return

    # Persist the mute so it survives a server restart (re-applied on login).
    try:
        player_row, _created_flag = await Player.objects.aget_or_create(
            unique_id=target_unique_id
        )
        await persist_mute(player_row, mute_for)
    except Exception as e:  # noqa: BLE001 - mute already applied live
        logger.warning(
            "Mute applied live but failed to persist for %s: %s",
            target_unique_id,
            e,
        )

    # Reflect the mute in the display-name tag immediately (no-op if the
    # character can't be resolved, e.g. stale mod roster entry).
    try:
        from amc.models import Character

        target_character = await Character.objects.filter(
            guid=target.get("CharacterGuid")
        ).afirst()
        if target_character is not None:
            await refresh_player_name(target_character, ctx.http_client_mod)
    except Exception as e:  # noqa: BLE001 - the tag is cosmetic, never fail the mute
        logger.warning(
            "Mute applied but failed to refresh name tag for %s: %s",
            target_unique_id,
            e,
        )

    if mute_for is True:
        duration_text = _("permanently")
    else:
        duration_text = _("for {seconds}s").format(seconds=mute_for)

    await ctx.reply(
        _("<Title>Player Muted</>\n\n{name} has been muted {duration}.").format(
            name=display_name, duration=duration_text
        )
    )


@registry.register(
    "/unmute",
    description=gettext_lazy("Unmute a player (Admin)"),
    category="Admin",
)
async def cmd_unmute(ctx: CommandContext, target_player_name: str):
    if not ctx.player_info or not ctx.player_info.get("bIsAdmin"):
        await ctx.reply(_("Admin-only"))
        return

    players = await get_players_mod(ctx.http_client_mod)
    if players is None:
        await ctx.reply(_("Could not fetch player list."))
        return
    target = next(
        (
            p
            for p in players
            if p.get("PlayerName") == target_player_name
            or strip_all_tags(p.get("PlayerName", "")) == target_player_name
        ),
        None,
    )
    if not target:
        await ctx.reply(_("Player '{name}' not found.").format(name=target_player_name))
        return

    target_unique_id = target.get("UniqueID")
    display_name = target.get("PlayerName", target_player_name)

    if not target_unique_id:
        await ctx.reply(_("Could not resolve player ID."))
        return

    try:
        await unmute_player(ctx.http_client_mod, target_unique_id)
    except Exception as e:
        await ctx.reply(_("Failed to unmute player: {error}").format(error=str(e)))
        return

    # Clear the persisted mute so it stays unmuted across restarts.
    try:
        player_row, _created_flag = await Player.objects.aget_or_create(
            unique_id=target_unique_id
        )
        await clear_persistent_mute(player_row)
    except Exception as e:  # noqa: BLE001 - unmute already applied live
        logger.warning(
            "Unmute applied live but failed to clear persistence for %s: %s",
            target_unique_id,
            e,
        )

    # Drop the mute tag from the display name immediately (no-op if the
    # character can't be resolved).
    try:
        from amc.models import Character

        target_character = await Character.objects.filter(
            guid=target.get("CharacterGuid")
        ).afirst()
        if target_character is not None:
            await refresh_player_name(target_character, ctx.http_client_mod)
    except Exception as e:  # noqa: BLE001 - the tag is cosmetic, never fail the unmute
        logger.warning(
            "Unmute applied but failed to refresh name tag for %s: %s",
            target_unique_id,
            e,
        )

    await ctx.reply(
        _("<Title>Player Unmuted</>\n\n{name} has been unmuted.").format(name=display_name)
    )


@registry.register(
    "/spawn_asset",
    description=gettext_lazy("Spawn an asset at your location (Admin)"),
    category="Admin",
)
async def cmd_spawn_asset(ctx: CommandContext, asset_path: str):
    if not ctx.player_info or not ctx.player_info.get("bIsAdmin"):
        await ctx.reply(_("Admin-only"))
        return

    player_data = await get_player(
        ctx.http_client_mod, str(ctx.player.unique_id), force_refresh=True
    )
    view_loc = player_data.get("ViewLocation") if player_data else None
    if view_loc:
        loc = {'X': view_loc['X'], 'Y': view_loc['Y'], 'Z': view_loc['Z']}
    else:
        loc = {
            'X': ctx.player_info["Location"]['X'],
            'Y': ctx.player_info["Location"]['Y'],
            'Z': ctx.player_info["Location"]['Z'] - 30,
        }
    rot = player_data.get("Rotation", {}) if player_data else {}
    yaw = rot.get("Yaw", 0.0)

    await spawn_assets(
        ctx.http_client_mod,
        [{"AssetPath": asset_path, "Location": loc, "Rotation": rot}],
    )

    world_obj = await WorldObject.objects.acreate(
        asset_path=asset_path,
        location_x=loc['X'],
        location_y=loc['Y'],
        location_z=loc['Z'],
        yaw=yaw,
    )
    await ctx.reply(
        _("Spawned asset: {path} (#{id})").format(
            path=asset_path, id=world_obj.pk
        )
    )


@registry.register(
    "/admin",
    description=gettext_lazy("Toggle admin status (test server only)"),
    category="Admin",
)
async def cmd_admin(ctx: CommandContext):
    from django.conf import settings

    if not settings.IS_TEST_SERVER:
        return

    is_admin = ctx.player_info and ctx.player_info.get("bIsAdmin", False)
    unique_id = str(ctx.player.unique_id)

    if is_admin:
        await remove_player_role(ctx.http_client, unique_id, "admin")
        await ctx.reply(
            _("<Title>Admin Removed</>\n\nYou are no longer an admin.")
        )
    else:
        await add_player_role(ctx.http_client, unique_id, "admin")
        await ctx.reply(
            _("<Title>Admin Granted</>\n\nYou are now an admin.")
        )


@registry.register(
    "/save_vehicle",
    description=gettext_lazy("Save your current vehicle with a name (Admin)"),
    category="Admin",
)
async def cmd_save_vehicle(ctx: CommandContext, name: str = None):
    if not ctx.player_info or not ctx.player_info.get("bIsAdmin"):
        await ctx.reply(_("Admin-only"))
        return

    if not name:
        await ctx.reply(_("<Title>Usage</>\n\n/save_vehicle [name]"))
        return

    results = await register_player_vehicles(
        ctx.http_client_mod, ctx.character, ctx.player
    )
    if not results:
        await ctx.reply(_("<Title>No Vehicle</>\n\nNo vehicle found to save."))
        return

    v = results[0]
    v.alias = name.strip()
    await v.asave(update_fields=["alias"])
    await ctx.reply(
        _("<Title>Vehicle Saved</>\n\n{vehicle_name} saved as <Bold>{alias}</> (#{id})").format(
            vehicle_name=v.config.get("VehicleName", "Vehicle"),
            alias=v.alias,
            id=v.id,
        )
    )


@registry.register(
    "/spawn_vehicle",
    description=gettext_lazy("Spawn a saved vehicle by name (Admin)"),
    category="Admin",
)
async def cmd_spawn_vehicle(ctx: CommandContext, name: str = None):
    if not ctx.player_info or not ctx.player_info.get("bIsAdmin"):
        await ctx.reply(_("Admin-only"))
        return

    if not name:
        await ctx.reply(_("<Title>Usage</>\n\n/spawn_vehicle [name]"))
        return

    v = await CharacterVehicle.objects.filter(
        character=ctx.character, alias__iexact=name.strip()
    ).afirst()
    if not v:
        await ctx.reply(
            _("<Title>Not Found</>\n\nNo saved vehicle with that name.")
        )
        return

    loc = {**ctx.player_info["Location"]}
    loc["Z"] -= 100
    await spawn_registered_vehicle(
        ctx.http_client_mod,
        v,
        loc,
        driver_guid=ctx.character.guid,
        tags=[ctx.character.name, "saved_vehicles"],
    )
    await ctx.reply(
        _("<Title>Vehicle Spawned</>\n\n{vehicle_name} (<Bold>{alias}</>)").format(
            vehicle_name=v.config.get("VehicleName", "Vehicle"),
            alias=v.alias,
        )
    )


def _validate_forced_name(new_name: str) -> str | None:
    """Return the cleaned forced name, or None if invalid."""
    clean = strip_all_tags(new_name).strip()
    # Reject empty / tag-only results too — a falsy forced_name would save
    # an inert lock ('') that neither blocks /rename nor reports via
    # /clear_forced_name, silently defeating the feature.
    if not clean or len(clean) > 20 or "(" in clean:
        return None
    return clean


async def _resolve_online_character_by_name(mod_session, target_player_name):
    """Find the online Character whose display name matches target_player_name."""
    players = await get_players_mod(mod_session)
    if not players:
        return None, None
    for p in players:
        pname = p.get("PlayerName") or ""
        if pname == target_player_name or strip_all_tags(pname) == target_player_name:
            guid = p.get("CharacterGuid")
            if not guid:
                continue
            try:
                character = await (
                    Character.objects.select_related("player").aget(guid=guid)
                )
            except Character.DoesNotExist:
                continue
            return character, p
    return None, None


async def _resolve_offline_player_by_name(target_player_name):
    """DB-only lookup of a Player by stored character name (exact or stripped).

    Dedupes by Player: if more than one distinct player account shares the
    given name, returns (None, None) rather than silently locking whichever
    character happened to sort first — an admin should not lock the wrong
    account on an ambiguous name.
    """
    stripped = strip_all_tags(target_player_name).strip()
    if not stripped:
        return None, None

    matched_player_ids = set()
    matched_characters = []

    async for character in (
        Character.objects.select_related("player").filter(name__iexact=stripped)
    ):
        matched_player_ids.add(character.player_id)
        matched_characters.append(character)

    if not matched_characters:
        matches = (
            Character.objects.select_related("player")
            .filter(name__icontains=stripped)
            .order_by("id")[:50]
        )
        async for character in matches:
            if strip_all_tags(character.name).lower() == stripped.lower():
                matched_player_ids.add(character.player_id)
                matched_characters.append(character)

    if len(matched_player_ids) != 1:
        # Ambiguous (or missing) — refuse to guess.
        return None, None

    player = await Player.objects.aget(pk=next(iter(matched_player_ids)))
    # Return the most recently stored matching character for that player.
    for character in reversed(matched_characters):
        if character.player_id == player.pk:
            return player, character
    return player, None


async def _resolve_player_for_force_rename(mod_session, target_player_name):
    """Resolve a Player (and an online Character, if any) for a force-rename.

    Tries the online mod player list first, then falls back to a DB lookup by
    stored character name so that offline players can still be locked.
    Shared by both the in-game and Discord admin commands.
    """
    character, _entry = await _resolve_online_character_by_name(
        mod_session, target_player_name
    )
    if character is not None:
        return character.player, character
    return await _resolve_offline_player_by_name(target_player_name)


@registry.register(
    "/force_rename",
    description=gettext_lazy(
        "Force rename a player (locked vs /rename & character switch) (Admin)"
    ),
    category="Admin",
)
async def cmd_force_rename(ctx: CommandContext, target_player_name: str, new_name: str):
    if not ctx.player_info or not ctx.player_info.get("bIsAdmin"):
        await ctx.reply(_("Admin-only"))
        return

    clean_name = _validate_forced_name(new_name)
    if clean_name is None:
        await ctx.reply(
            _(
                "<Title>Invalid Name</>\n\nNames must be at most 20 characters and cannot contain '('."
            )
        )
        return

    target_player, character = await _resolve_player_for_force_rename(
        ctx.http_client_mod, target_player_name
    )
    if target_player is None:
        await ctx.reply(
            _("Player '{name}' not found.").format(name=target_player_name)
        )
        return

    old_name = target_player.forced_name
    target_player.forced_name = clean_name
    await target_player.asave(update_fields=["forced_name"])

    await log_forced_name_change(
        target_player,
        action="set",
        old_name=old_name,
        new_name=clean_name,
        actor_character=ctx.character,
        actor_player=ctx.player,
    )

    # Re-apply immediately so the change is visible without a re-login
    # (no-op if the player is offline).
    if character is not None:
        await refresh_player_name(character, ctx.http_client_mod)

    await ctx.reply(
        _(
            "<Title>Forced Rename</>\n\n{old} is now forced to the name <Bold>{new}</>. They cannot change it via /rename or by switching characters."
        ).format(new=clean_name, old=target_player_name)
    )
    await ctx.announce(
        f"{ctx.character.name} force-renamed {target_player_name} to {clean_name}."
    )


@registry.register(
    "/clear_forced_name",
    description=gettext_lazy("Remove an admin-imposed name lock (Admin)"),
    category="Admin",
)
async def cmd_clear_forced_name(ctx: CommandContext, target_player_name: str):
    if not ctx.player_info or not ctx.player_info.get("bIsAdmin"):
        await ctx.reply(_("Admin-only"))
        return

    target_player, character = await _resolve_player_for_force_rename(
        ctx.http_client_mod, target_player_name
    )
    if target_player is None:
        await ctx.reply(
            _("Player '{name}' not found.").format(name=target_player_name)
        )
        return

    if not target_player.forced_name:
        await ctx.reply(
            _("{name} does not have a forced name.").format(name=target_player_name)
        )
        return

    old_name = target_player.forced_name
    target_player.forced_name = None
    await target_player.asave(update_fields=["forced_name"])

    await log_forced_name_change(
        target_player,
        action="clear",
        old_name=old_name,
        new_name=None,
        actor_character=ctx.character,
        actor_player=ctx.player,
    )

    # Restore their chosen name (no-op if offline).
    if character is not None:
        await refresh_player_name(character, ctx.http_client_mod)

    await ctx.reply(
        _("<Title>Name Lock Removed</>\n\n{name} can now choose their own name again.").format(
            name=target_player_name
        )
    )
    await ctx.announce(
        f"{ctx.character.name} removed the forced name on {target_player_name}."
    )
