from amc.command_framework import registry, CommandContext
from amc.models import VehicleDecal
from amc.mod_server import get_decal, set_decal
from django.db.models import Q
from django.utils.translation import gettext as _, gettext_lazy

@registry.register("/decals", description=gettext_lazy("List your saved decals"), category="Decals")
async def cmd_decals(ctx: CommandContext):
    qs = VehicleDecal.objects.filter(player=ctx.player)
    decals = '\n'.join([
        f"#{decal.hash[:8]} - {decal.name} ({decal.vehicle_key})"
        async for decal in qs
    ])
    await ctx.reply(f"""<Title>Your Decals</>
-<Highlight>/apply_decal [name_or_hash]</> to apply an decal
-<Highlight>/save_decal [name_or_hash]</> while in a vehicle to save its decal

<Bold>Your decals:</>
{decals}""")

@registry.register("/save_decal", description=gettext_lazy("Save your current vehicle decal"), category="Decals")
async def cmd_save_decal(ctx: CommandContext, decal_name: str):
    decal_config = await get_decal(ctx.http_client_mod, player_id=str(ctx.player.unique_id))
    hash_val = VehicleDecal.calculate_hash(decal_config)
    
    # We need player info for VehicleKey, assumed accessible via ctx.player_info or fetching
    vehicle_key = ctx.player_info.get('VehicleKey', 'Unknown') if ctx.player_info else 'Unknown'

    decal = await VehicleDecal.objects.acreate(
        name=decal_name,
        player=ctx.player,
        config=decal_config,
        hash=hash_val,
        vehicle_key=vehicle_key
    )
    await ctx.reply(f"""<Title>Decal Saved!</>
{decal.name} has been saved.
ID: <Event>{hash_val[:8]}</>
Apply with: <Highlight>/apply_decal {decal.name}</>
See all: <Highlight>/decals</>""")

@registry.register("/apply_decal", description=gettext_lazy("Apply a saved decal"), category="Decals")
async def cmd_apply_decal(ctx: CommandContext, decal_name: str):
    try:
        decal = await VehicleDecal.objects.aget(
            Q(name=decal_name) | Q(hash=decal_name),
            Q(player=ctx.player) | Q(private=False),
        )
    except VehicleDecal.DoesNotExist:
        qs = VehicleDecal.objects.filter(player=ctx.player)
        decals = '\n'.join([f"#{d.hash} - {d.name} ({d.vehicle_key})" async for d in qs])
        await ctx.reply(f"<Title>Decal not found</>\n\n{decals}")
        return
    await set_decal(ctx.http_client_mod, str(ctx.player.unique_id), decal.config)
