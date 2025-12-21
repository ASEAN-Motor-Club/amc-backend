import asyncio
from amc.command_framework import registry, CommandContext
from amc.mod_server import (
    despawn_player_vehicle, show_popup, despawn_by_tag,
    spawn_garage, spawn_assets, spawn_vehicle,
    force_exit_vehicle, get_players as get_players_mod
)
from amc.game_server import get_players2
from amc.vehicles import spawn_registered_vehicle
from amc.models import (
    CharacterVehicle, VehicleDealership, WorldText, WorldObject, Garage
)
from amc.enums import VehicleKey
from django.utils.translation import gettext as _, gettext_lazy

@registry.register(["/despawn", "/d"], description=gettext_lazy("Despawn your vehicle"), category="Vehicle Management")
async def cmd_despawn(ctx: CommandContext, category: str = "all"):
    # Feature disabled logic from original
    await ctx.reply(_("<Title>Feature disabled</>\n\nSorry, this feature has been permanently disabled."))

@registry.register("/admin_despawn", description=gettext_lazy("Admin despawn tool"), category="Admin")
async def cmd_admin_despawn(ctx: CommandContext, category: str = "all"):
    if not ctx.player_info.get('bIsAdmin'):
        asyncio.create_task(show_popup(ctx.http_client_mod, _("<Title>Feature disabled</>\n\nSorry, this feature is temporarily unavailable"), character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id)))
        return
    
    if category != "all" and category != "others":
        players = await get_players2(ctx.http_client)
        target_guid = next((p.get('character_guid') for pid, p in players if p.get('name', '').startswith(category)), None)
        
        if target_guid:
            await despawn_player_vehicle(ctx.http_client_mod, target_guid, category='others')
            asyncio.create_task(show_popup(ctx.http_client_mod, _("<Title>Player vehicles despawned</>\n\n"), character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id)))
            return
        else:
             asyncio.create_task(show_popup(ctx.http_client_mod, _("<Title>Player not found</>\n\nPlease make sure you typed the name correctly."), character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id)))
             return

    await despawn_player_vehicle(ctx.http_client_mod, ctx.player.unique_id, category=category)

@registry.register("/spawn_displays", description=gettext_lazy("Spawn display vehicles"), category="Admin")
async def cmd_spawn_displays(ctx: CommandContext, display_id: int = None):
    if not ctx.player_info.get('bIsAdmin'): return
    qs = CharacterVehicle.objects.select_related('character').filter(spawn_on_restart=True)
    if display_id: qs = qs.filter(pk=display_id)
    
    async for v in qs:
        tags = [f'display-{v.id}']
        if v.character: tags.extend([v.character.name, f"display-{v.character.guid}"])
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
        async for wt in WorldText.objects.all(): await spawn_assets(ctx.http_client_mod, wt.generate_asset_data())
        async for wo in WorldObject.objects.all(): await spawn_assets(ctx.http_client_mod, [wo.generate_asset_data()])

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
