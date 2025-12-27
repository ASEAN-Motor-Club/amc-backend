import asyncio
from amc.command_framework import registry, CommandContext
from amc.mod_server import (
    show_popup, despawn_by_tag,
    spawn_garage, spawn_assets, spawn_vehicle,
    force_exit_vehicle, get_players as get_players_mod,
    teleport_player
)
from amc.game_server import get_players
from amc.vehicles import spawn_registered_vehicle
from amc.models import (
    CharacterVehicle, VehicleDealership, WorldText, WorldObject, Garage,
    TeleportPoint
)
from amc.enums import VehicleKey
from django.utils.translation import gettext as _, gettext_lazy
from amc.utils import fuzzy_find_player


@registry.register("/spawn_displays", description=gettext_lazy("Spawn display vehicles"), category="Admin")
async def cmd_spawn_displays(ctx: CommandContext, display_id: int = None):
    if not ctx.player_info.get('bIsAdmin'):
        return
    qs = CharacterVehicle.objects.select_related('character').filter(spawn_on_restart=True)
    if display_id:
        qs = qs.filter(pk=display_id)
    
    async for v in qs:
        tags = [f'display-{v.id}']
        if v.character:
            tags.extend([v.character.name, f"display-{v.character.guid}"])
        await despawn_by_tag(ctx.http_client_mod, f'display-{v.id}')
        await spawn_registered_vehicle(
            ctx.http_client_mod, v, tag="display_vehicles", 
            extra_data={'companyName': f"{v.character.name}'s Display", 'drivable': v.rental} if v.character else {},
            tags=tags
        )

@registry.register("/spawn_dealerships", description=gettext_lazy("Spawn dealership vehicles"), category="Admin")
async def cmd_spawn_dealerships(ctx: CommandContext):
    if ctx.player_info.get('bIsAdmin'):
        async for vd in VehicleDealership.objects.filter(spawn_on_restart=True):
            await vd.spawn(ctx.http_client_mod)

@registry.register("/spawn_assets", description=gettext_lazy("Spawn world assets"), category="Admin")
async def cmd_spawn_assets(ctx: CommandContext):
    if ctx.player_info.get('bIsAdmin'):
        async for wt in WorldText.objects.all():
            await spawn_assets(ctx.http_client_mod, wt.generate_asset_data())
        async for wo in WorldObject.objects.all():
            await spawn_assets(ctx.http_client_mod, [wo.generate_asset_data()])

@registry.register("/spawn_garages", description=gettext_lazy("Spawn garages"), category="Admin")
async def cmd_spawn_garages(ctx: CommandContext):
    if ctx.player_info.get('bIsAdmin'):
        async for g in Garage.objects.filter(spawn_on_restart=True):
            resp = await spawn_garage(ctx.http_client_mod, g.config['Location'], g.config['Rotation'])
            g.tag = resp.get('tag')
            await g.asave()

@registry.register("/spawn_garage", description=gettext_lazy("Spawn a single garage"), category="Admin")
async def cmd_spawn_garage_single(ctx: CommandContext, name: str):
    if ctx.player_info.get('bIsAdmin'):
        loc = ctx.player_info['Location']
        loc['Z'] -= 100
        rot = ctx.player_info.get('Rotation', {})
        resp = await spawn_garage(ctx.http_client_mod, loc, rot)
        tag = resp.get('tag')
        await ctx.announce(_("Garage spawned! Tag: {tag}").format(tag=tag))
        await Garage.objects.acreate(config={'Location': loc, 'Rotation': rot}, notes=name.strip(), tag=tag)

@registry.register("/spawn", description=gettext_lazy("Spawn a vehicle"), category="Admin")
async def cmd_spawn(ctx: CommandContext, vehicle_label: str = None):
    if not ctx.player_info.get('bIsAdmin'):
        await ctx.reply(_("Admin-only"))
        return
    
    if not vehicle_label:
        await ctx.reply(_("<Title>Spawn Vehicle</>\n\n") + "\n".join(VehicleKey.labels))
    elif vehicle_label.isdigit():
        vehicle = await CharacterVehicle.objects.aget(pk=int(vehicle_label))
        loc = ctx.player_info['Location']
        loc['Z'] -= 5
        await spawn_registered_vehicle(ctx.http_client_mod, vehicle, loc, driver_guid=ctx.character.guid, tags=['spawned_vehicles'])
    else:
        await spawn_vehicle(ctx.http_client_mod, vehicle_label, ctx.player_info['Location'], driver_guid=ctx.character.guid)

@registry.register("/exit", description=gettext_lazy("Force exit vehicle (Admin)"), category="Admin")
async def cmd_exit(ctx: CommandContext, target_player_name: str):
    if ctx.player_info.get('bIsAdmin'):
        players = await get_players_mod(ctx.http_client_mod)
        target_guid = next((p['CharacterGuid'] for p in players if p['PlayerName'] == target_player_name), None)
        if target_guid:
            await force_exit_vehicle(ctx.http_client_mod, target_guid)


@registry.register("/tp_player", description=gettext_lazy("Teleport a player to a location (Admin)"), category="Admin")
async def cmd_tp_player(ctx: CommandContext, target_player_name: str, location_name: str):
    if not ctx.player_info.get('bIsAdmin'):
        await ctx.reply(_("Admin-only"))
        return

    # Find the target player
    players = await get_players(ctx.http_client)
    target_pid = fuzzy_find_player(players, target_player_name)

    if not target_pid:
        asyncio.create_task(show_popup(ctx.http_client_mod, _("<Title>Player not found</>\n\nPlease make sure you typed the name correctly."), character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id)))
        return
    
    # Find the location
    try:
        teleport_point = await TeleportPoint.objects.aget(name__iexact=location_name)
        loc_obj = teleport_point.location
        location = {'X': loc_obj.x, 'Y': loc_obj.y, 'Z': loc_obj.z}
    except TeleportPoint.DoesNotExist:
        tp_points = TeleportPoint.objects.filter(character__isnull=True).order_by('name')
        tp_points_names = [tp.name async for tp in tp_points]
        asyncio.create_task(show_popup(ctx.http_client_mod, _("Teleport point not found\nChoose from one of the following locations:\n\n{locations}").format(
            locations='\n'.join(tp_points_names)
        ), character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id)))
        return

    # Teleport
    await teleport_player(
        ctx.http_client_mod,
        str(target_pid),
        location,
        no_vehicles=False, # Admins might want to move vehicles too, or maybe not. Defaulting to False (move vehicle) as it's often useful.
        reset_trailers=False,
        reset_carried_vehicles=False,
    )
    await ctx.reply(_("Teleported {player} to {location}").format(player=target_player_name, location=location_name))
