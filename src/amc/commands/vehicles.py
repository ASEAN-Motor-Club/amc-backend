from amc.command_framework import registry, CommandContext
from amc.models import CharacterVehicle
from amc.vehicles import (
    register_player_vehicles, spawn_registered_vehicle, format_key_string
)
from amc.mod_server import despawn_by_tag, despawn_player_vehicle, show_popup
import asyncio
import itertools
from django.utils.translation import gettext as _, gettext_lazy

@registry.register(["/despawn", "/d"], description=gettext_lazy("Despawn your vehicle"), category="Vehicle Management")
async def cmd_despawn(ctx: CommandContext, category: str = "all"):
    # Handled by server mod, we just register so it shows up on /help
    pass

@registry.register("/register_vehicles", description=gettext_lazy("Register your vehicles"), category="Vehicle Management")
async def cmd_register_vehicles(ctx: CommandContext):
    vehicles = await register_player_vehicles(ctx.http_client_mod, ctx.character, ctx.player)
    names = '\n'.join([f"#{v.id} - {v.config['VehicleName']}" for v in vehicles]) if vehicles else _('No vehicles found')
    await ctx.reply(_("<Title>Vehicles Registered!</>\n\n{names}").format(names=names))

@registry.register("/unrental", description=gettext_lazy("Stop renting out your vehicle"), category="Vehicle Management")
async def cmd_unrental(ctx: CommandContext, category: str = ""):
    category = category.strip()
    if category == 'all':
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
            await despawn_by_tag(ctx.http_client_mod, f'rental-{v.id}')
            v.rental = False
            await v.asave()
    await ctx.reply(_("Rentals removed"))

@registry.register("/rental", description=gettext_lazy("Mark vehicle as for rental"), category="Vehicle Management")
async def cmd_rental(ctx: CommandContext, alias: str = ""):
    vehicles = await register_player_vehicles(ctx.http_client_mod, ctx.character, ctx.player, active=True)
    # Filter for corp vehicles
    own_company_guid = ctx.player_info.get('OwnCompanyGuid') if ctx.player_info else None
    vehicles = [v for v in vehicles if v.config.get('CompanyName') and v.company_guid == own_company_guid] if vehicles else []
    
    if not vehicles:
        await ctx.reply(_("<Title>Rental System</>\nOnly Corporation vehicles can be rented out."))
        return

    for v in vehicles:
        if not v.rental:
            v.rental = True
        if alias.strip():
            v.alias = alias.strip()
        await v.asave()
    
    names = '\n'.join([f"<Small>#{v.id} - {v.config['VehicleName']}</>" for v in vehicles if v.rental])
    await ctx.reply(_("<Title>Marked as rental</>\nPlayers can /rent these:\n\n{names}").format(names=names))

@registry.register("/rent", description=gettext_lazy("Rent a vehicle"), category="Vehicle Management")
async def cmd_rent(ctx: CommandContext, vehicle_id: str = ""):
    # Logic to show list if vehicle_id is text/empty, or spawn if ID
    if not vehicle_id or not vehicle_id.isdigit():
        # List logic
        vehicles = [v async for v in CharacterVehicle.objects.filter(rental=True)]
        if vehicle_id.strip():
            # If search term provided
            search = vehicle_id.strip().lower()
            vehicles = [
                v for v in vehicles 
                if search in format_key_string(v.config['VehicleName']).lower()
            ]
        
        if not vehicles:
            await ctx.reply(_("<Title>Rentals</>\nNo rentals found."))
            return

        # Group by company
        vehicles.sort(key=lambda v: v.config.get('CompanyName', 'Independent'))
        
        lines: list[str] = []
        for company, group in itertools.groupby(vehicles, key=lambda v: v.config.get('CompanyName', 'Independent')):
            lines.append(f"<Bold>{company}</>")
            for v in group:
                lines.append(f" <Small>#{v.id} - {v.config['VehicleName']}</>")
            lines.append("")

        names = '\n'.join(lines)
        await ctx.reply(_("<Title>Available Rentals</>\nType /rent [id] to rent.\n\n{names}").format(names=names))
    else:
        # Spawn logic
        try:
            v = await CharacterVehicle.objects.aget(pk=vehicle_id, rental=True)
            if not ctx.player_info:
                await ctx.reply(_("Player info not found"))
                return
            loc = ctx.player_info['Location']
            loc['Z'] -= 100
            await spawn_registered_vehicle(ctx.http_client_mod, v, loc, driver_guid=ctx.character.guid, 
                                           tags=[ctx.character.name, 'rental_vehicles', f'rental-{v.id}'])
            await ctx.reply(_("Brought to you by {company}").format(company=v.config.get('CompanyName')))
        except CharacterVehicle.DoesNotExist:
            await ctx.reply(_("Rental not found"))

@registry.register("/sell", description=gettext_lazy("Sell a vehicle"), category="Vehicle Management")
async def cmd_sell(ctx: CommandContext):
    if not ctx.player_info or not ctx.player_info.get('bIsAdmin'):
        return
    vehicles = await register_player_vehicles(ctx.http_client_mod, ctx.character, ctx.player, active=True)
    await despawn_player_vehicle(ctx.http_client_mod, ctx.player.unique_id)
    if vehicles:
        for v in vehicles:
            await despawn_by_tag(ctx.http_client_mod, f'sale-{v.id}')
        v.for_sale = True
        await v.asave()
        await spawn_registered_vehicle(ctx.http_client_mod, v, v.config['Location'], rotation=v.config['Rotation'], 
                                       for_sale=True, driver_guid=ctx.character.guid, tags=['sale_vehicles'])

@registry.register("/undisplay", description=gettext_lazy("Remove displayed vehicles"), category="Vehicle Management")
async def cmd_undisplay(ctx: CommandContext, category: str = ""):
    if (not ctx.player_info or not ctx.player_info.get('bIsAdmin')) and not ctx.player.displayer:
        asyncio.create_task(
            show_popup(ctx.http_client_mod, _("Admin-only command"), character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id))
        )
        return

    if category.strip() == 'all':
        vehicles = [v async for v in CharacterVehicle.objects.filter(character=ctx.character, spawn_on_restart=True)]
    else:
        vehicles = await register_player_vehicles(ctx.http_client_mod, ctx.character, ctx.player, active=True)

    if not vehicles:
        asyncio.create_task(
            show_popup(ctx.http_client_mod, _("""<Title>Display System</>
Use <Highlight>/undisplay </> to remove displayed vehicles."""),
            character_guid=ctx.character.guid,
            player_id=str(ctx.player.unique_id))
        )
        return

    for v in vehicles:
        if v.spawn_on_restart:
            await despawn_by_tag(ctx.http_client_mod, f'display-{v.id}')
            v.spawn_on_restart = False
            await v.asave(update_fields=['spawn_on_restart'])
    
    await ctx.reply(_("Undisplay complete"))

@registry.register("/display", description=gettext_lazy("Permanently display a vehicle"), category="Vehicle Management")
async def cmd_display(ctx: CommandContext, category: str = ""):
    if (not ctx.player_info or not ctx.player_info.get('bIsAdmin')) and not ctx.player.displayer:
        asyncio.create_task(
            show_popup(ctx.http_client_mod, _("Admin-only command"), character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id))
        )
        return

    active = True
    if category.strip() == 'all':
        active = None
        
    vehicles = await register_player_vehicles(ctx.http_client_mod, ctx.character, ctx.player, active=active)
    if not vehicles:
        asyncio.create_task(
            show_popup(ctx.http_client_mod, _("""<Title>Display System</>
Use <Highlight>/display </> to permanently display a vehicle"""),
            character_guid=ctx.character.guid,
            player_id=str(ctx.player.unique_id))
        )
        return

    await asyncio.sleep(0.5)
    # Note: Using ctx.player.unique_id for despawn, verify string/int requirements of mod_server funcs
    # tasks.py used player_id passed from event
    await despawn_player_vehicle(ctx.http_client_mod, str(ctx.player.unique_id), category=category.strip() or 'others') 
    # Logic in tasks.py: category=command_match.group('category')
    # If category is empty, regex group is empty string.
    
    await asyncio.sleep(0.5)

    for v in vehicles:
        if v.spawn_on_restart:
            await despawn_by_tag(ctx.http_client_mod, f'display-{v.id}')
        v.spawn_on_restart = True
        await v.asave(update_fields=['spawn_on_restart'])
        await spawn_registered_vehicle(
            ctx.http_client_mod,
            v,
            v.config['Location'],
            rotation=v.config['Rotation'],
            for_sale=False,
            extra_data={
                'companyGuid': '1'*32,
                'companyName': f"{ctx.character.name}'s Display",
                'drivable': False,
            },
            tags=[ctx.character.name, 'display_vehicles', f'display-{v.id}']
        )

    '\n'.join([
        f"<Small>#{v.id} - {v.config['VehicleName']}</>"
        for v in vehicles
    ])
    asyncio.create_task(
        show_popup(
            ctx.http_client_mod,
            _("""
<Title>Successfully marked as display</>

Your vehicle will be automatically spawned here when the server starts!

To change the position, you will need to do /display again with the same vehicle."""),
            character_guid=ctx.character.guid,
            player_id=str(ctx.player.unique_id)
        )
    )
