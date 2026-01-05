import asyncio
from amc.command_framework import registry, CommandContext
from amc.models import RescueRequest
from django.contrib.gis.geos import Point
from amc.mod_server import (
    get_rp_mode, set_character_name,
    get_players as get_players_mod, list_player_vehicles,
    send_system_message
)
from amc.vehicles import format_key_string
from django.utils import timezone
from datetime import timedelta
from django.conf import settings
from django.utils.translation import gettext as _, gettext_lazy
from amc.mod_server import get_player

@registry.register(["/rp_mode", "/rp"], description=gettext_lazy("Toggle Roleplay Mode"), category="RP & Rescue") # type: ignore
async def cmd_rp_mode(ctx: CommandContext, verification_code: str = ""):
    if verification_code:
        is_rp_mode = await get_rp_mode(ctx.http_client_mod, ctx.character.guid)
        ctx.character.rp_mode = is_rp_mode
        await ctx.character.asave(update_fields=['rp_mode'])
        
        # Name Update Logic
        new_name = None
        if is_rp_mode and '[RP]' not in ctx.character.name:
            new_name = f"{ctx.character.name}[RP]"
        elif not is_rp_mode and '[RP]' in ctx.character.name:
            new_name = ctx.character.name.replace('[RP]', '')
        if new_name:
            await set_character_name(ctx.http_client_mod, ctx.character.guid, new_name)
        


@registry.register("/rescue", description=gettext_lazy("Calls for rescue service"), category="RP & Rescue", featured=True) # type: ignore
async def cmd_rescue(ctx: CommandContext, message: str = ""):
    if await RescueRequest.objects.filter(character=ctx.character, timestamp__gte=timezone.now() - timedelta(minutes=5)).aexists():
        await ctx.reply(_("You have requested a rescue less than 5 minutes ago"))
        return
    
    # 1. Notify In-Game Rescuers
    players = await get_players_mod(ctx.http_client_mod)
    vehicles = await list_player_vehicles(ctx.http_client_mod, ctx.player.unique_id, active=True)
    vehicle_names = '+'.join([format_key_string(v.get('VehicleName', '?')) for v in vehicles.values()]) if vehicles else _('Vehicles')
    
    sent = False
    if players:
        for p in players:
            if '[ARWRS]' in p.get('PlayerName', '') or '[DOT]' in p.get('PlayerName', ''): 
                asyncio.create_task(send_system_message(
                    ctx.http_client_mod, 
                    _("{name} needs help!").format(
                        name=ctx.character.name
                    ),
                    character_guid=p.get('CharacterGuid'),
                ))
                sent = True
    
    # 2. Create DB Entry
    location = None
    player_info = await get_player(ctx.http_client_mod, str(ctx.player.unique_id))
    if player_info and 'Location' in player_info:
        loc = player_info['Location']
        location = Point(loc['X'], loc['Y'], loc['Z'], srid=0)
    
    rescue_request = await RescueRequest.objects.acreate(character=ctx.character, message=message, location=location)
    
    if ctx.is_current_event:
        await ctx.announce(_("{name} needs a rescue! {vehicle_names}. Respond with /respond {request_id}").format(
            name=ctx.character.name, vehicle_names=vehicle_names, request_id=rescue_request.id
        ))
        await ctx.reply(_("<EffectGood>Request Sent>\n") + (_("Help is on the way.") if sent else _("Rescuers offline, notified Discord.")))

    # 3. Discord Notification
    if ctx.discord_client:
        async def send_discord():
            from amc.utils import forward_to_discord
            msg = await forward_to_discord(
                ctx.discord_client,
                settings.DISCORD_RESCUE_CHANNEL_ID,
                _("@here **{name}** requested rescue.\nMsg: {message}").format(name=ctx.character.name, message=message),
                escape_mentions=False,
                silent=True
            )
            if msg:
                rescue_request.discord_message_id = msg.id
                await rescue_request.asave()
        asyncio.run_coroutine_threadsafe(send_discord(), ctx.discord_client.loop)

@registry.register("/respond", description=gettext_lazy("Respond to a rescue request"), category="RP & Rescue")
async def cmd_respond(ctx: CommandContext, rescue_id: int):
    try:
        rescue_request = await RescueRequest.objects.select_related('character').aget(pk=rescue_id)
    except RescueRequest.DoesNotExist:
        try:
            rescue_request = await RescueRequest.objects.select_related('character').aget(timestamp__gte=timezone.now() - timedelta(minutes=5))
        except Exception:
            await ctx.reply(_("Invalid or expired rescue request."))
            return

    await rescue_request.responders.aadd(ctx.player)

    await ctx.announce(_("{responder} is responding to {requester}'s rescue request!").format(
        responder=ctx.character.name, requester=rescue_request.character.name
    ))

    await ctx.reply(_(
        "<Title>Rescue Response</>\n"
        "You are responding to {requester}'s rescue!\n\n"
        "<EffectGood>Teleport Enabled</>\n"
        "Use <Highlight>/tp</> with your custom destination marker "
        "to teleport within 10,000 units of {requester} for the next 10 minutes."
    ).format(requester=rescue_request.character.name))
    
    # Discord Reaction
    if ctx.discord_client and rescue_request.discord_message_id:
        roleplay_cog = ctx.discord_client.get_cog('RoleplayCog')
        if roleplay_cog:
            asyncio.run_coroutine_threadsafe(
                roleplay_cog.add_reaction_to_rescue_message(rescue_request.discord_message_id, '👍'),
                ctx.discord_client.loop
            )
