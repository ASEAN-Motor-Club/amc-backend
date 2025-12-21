import asyncio
from amc.command_framework import registry, CommandContext
from amc.models import TeleportPoint
from amc.mod_server import teleport_player, list_player_vehicles, show_popup
from django.db.models import Q

@registry.register(["/teleport", "/tp"], description="Teleport to coordinates (Admin Only)", category="Teleportation")
async def cmd_tp_coords(ctx: CommandContext, x: int, y: int, z: int):
    if ctx.player_info.get('bIsAdmin'):
        await teleport_player(ctx.http_client_mod, ctx.player.unique_id, {'X': x, 'Y': y, 'Z': z}, no_vehicles=False)
    else:
        await ctx.reply("Admin Only")

@registry.register(["/teleport", "/tp"], description="Teleport to a location", category="Teleportation")
async def cmd_tp_name(ctx: CommandContext, name: str = ""):
    CORPS_WITH_TP = { "69FF57844F3F79D1F9665991B4006325" }
    player_info = ctx.player_info or {} 
    
    tp_points = TeleportPoint.objects.filter(character__isnull=True).order_by('name')
    tp_points_names = [tp.name async for tp in tp_points]
    
    current_vehicle = None
    try:
        player_vehicles = await list_player_vehicles(ctx.http_client_mod, ctx.player.unique_id, active=True)
        if isinstance(player_vehicles, dict):
            for vehicle_id, vehicle in player_vehicles.items():
                if vehicle.get('index') == 0:
                    current_vehicle = vehicle
    except Exception:
        pass

    no_vehicles = not player_info.get('bIsAdmin')
    location = None

    if name:
        try:
            teleport_point = await TeleportPoint.objects.aget(
                Q(character=ctx.character) | Q(character__isnull=True),
                name__iexact=name,
            )
            loc_obj = teleport_point.location
            location = {'X': loc_obj.x, 'Y': loc_obj.y, 'Z': loc_obj.z}
        except TeleportPoint.DoesNotExist:
            asyncio.create_task(show_popup(ctx.http_client_mod, f"Teleport point not found\nChoose from one of the following locations:\n\n{'\n'.join(tp_points_names)}", character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id)))
            return
    elif player_info.get('bIsAdmin') or (current_vehicle and current_vehicle.get('companyGuid') in CORPS_WITH_TP):
        # Teleport to Custom Waypoint
        no_vehicles = False
        location = player_info.get('CustomDestinationAbsoluteLocation')
        if location:
            # Fix Z offset based on vehicle
            if player_info.get('VehicleKey') == 'None':
                location['Z'] += 100
            else:
                location['Z'] += 5
    
    if not location:
        asyncio.create_task(
            show_popup(ctx.http_client_mod, f"<Title>Teleport</>\nUsage: <Highlight>/tp [location]</>\nChoose from one of the following locations:\n\n{'\n'.join(tp_points_names)}", character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id))
        )
        return

    await teleport_player(
        ctx.http_client_mod,
        ctx.player.unique_id,
        location,
        no_vehicles=no_vehicles,
        reset_trailers=not player_info.get('bIsAdmin'),
        reset_carried_vehicles=not player_info.get('bIsAdmin'),
    )
