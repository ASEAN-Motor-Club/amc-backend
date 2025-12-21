import asyncio
from amc.command_framework import registry, CommandContext
from amc.models import RescueRequest
from amc.mod_server import (
    get_rp_mode, despawn_player_vehicle,
    toggle_rp_session, despawn_by_tag, set_character_name,
    get_players as get_players_mod, list_player_vehicles,
    show_popup
)
from amc.utils import with_verification_code
from amc.vehicles import format_key_string
from django.utils import timezone
from datetime import timedelta
from django.conf import settings

@registry.register(["/rp_mode", "/rp"], description="Toggle Roleplay Mode", category="RP & Rescue")
async def cmd_rp_mode(ctx: CommandContext, verification_code: str = None):
    is_rp_mode = await get_rp_mode(ctx.http_client_mod, ctx.character.guid)
    
    if verification_code:
        # Verify and Toggle
        code_gen, code_verified = with_verification_code((ctx.character.guid, is_rp_mode), verification_code)
        if not code_verified:
            await ctx.reply(f"<Title>Code Incorrect</>\nTry: <Highlight>/rp_mode {code_gen.upper()}</>")
            return
        
        await despawn_player_vehicle(ctx.http_client_mod, ctx.player.unique_id)
        try:
            await toggle_rp_session(ctx.http_client_mod, ctx.character.guid)
        except Exception:
            pass
        await despawn_by_tag(ctx.http_client_mod, f'rental-{ctx.character.guid}')
        
        # Refresh State
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
        
        await asyncio.sleep(1)
        msg = "<Title>Roleplay Mode Enabled</>\n<EffectGood>Enabled!</>" if is_rp_mode else "<Title>Roleplay Mode Disabled</>"
        await ctx.reply(msg)

    else:
        # Request Confirmation
        code_gen, _ = with_verification_code((ctx.character.guid, is_rp_mode), "")
        status = '<EffectGood>ON</>' if is_rp_mode else '<Warning>OFF</>'
        notes = "To turn off, resend with code:" if is_rp_mode else "Enabling RP mode gives bonuses but risks cargo/vehicle loss on recovery.\n<Warning>All vehicles will despawn on toggle!</>"
        await ctx.reply(f"<Title>Roleplay Mode</>\nStatus: {status}\n\n{notes}\n<Highlight>/rp_mode {code_gen.upper()}</>")

@registry.register("/rescue", description="Calls for rescue service", category="RP & Rescue")
async def cmd_rescue(ctx: CommandContext, message: str = ""):
    if await RescueRequest.objects.filter(character=ctx.character, timestamp__gte=timezone.now() - timedelta(minutes=5)).aexists():
        await ctx.reply("You have requested a rescue less than 5 minutes ago")
        return
    
    # 1. Notify In-Game Rescuers
    players = await get_players_mod(ctx.http_client_mod)
    vehicles = await list_player_vehicles(ctx.http_client_mod, ctx.player.unique_id, active=True)
    vehicle_names = '+'.join([format_key_string(v.get('VehicleName', '?')) for v in vehicles.values()])
    
    sent = False
    for p in players:
        if '[ARWRS]' in p.get('PlayerName', '') or '[DOT]' in p.get('PlayerName', ''): 
            asyncio.create_task(show_popup(
                ctx.http_client_mod, 
                f"<Title>Rescue Request</>\n<Event>{ctx.character.name}</> needs help!\nMsg: {message}\nVeh: {vehicle_names}",
                character_guid=p.get('CharacterGuid'),
                player_id=str(ctx.player.unique_id)
            ))
            sent = True
    
    # 2. Create DB Entry
    rescue_request = await RescueRequest.objects.acreate(character=ctx.character, message=message)
    
    if ctx.is_current_event:
        await ctx.announce(f"{ctx.character.name} needs a rescue! {vehicle_names}. /respond {rescue_request.id}")
        await ctx.reply("<EffectGood>Request Sent</>\n" + ("Help is on the way." if sent else "Rescuers offline, notified Discord."))

    # 3. Discord Notification
    if ctx.discord_client:
        async def send_discord():
            from amc.utils import forward_to_discord
            msg = await forward_to_discord(
                ctx.discord_client,
                settings.DISCORD_RESCUE_CHANNEL_ID,
                f"@here **{ctx.character.name}** requested rescue.\nMsg: {message}",
                escape_mentions=False
            )
            if msg:
                rescue_request.discord_message_id = msg.id
                await rescue_request.asave()
        asyncio.run_coroutine_threadsafe(send_discord(), ctx.discord_client.loop)

@registry.register("/respond", description="Respond to a rescue request", category="RP & Rescue")
async def cmd_respond(ctx: CommandContext, rescue_id: int):
    try:
        rescue_request = await RescueRequest.objects.select_related('character').aget(pk=rescue_id)
    except RescueRequest.DoesNotExist:
         try:
             rescue_request = await RescueRequest.objects.select_related('character').aget(timestamp__gte=timezone.now() - timedelta(minutes=5))
         except:
             await ctx.reply("Invalid or expired rescue request.")
             return

    await rescue_request.responders.aadd(ctx.player)
    await ctx.announce(f"{ctx.character.name} responded to {rescue_request.character.name}'s request!")
    
    # Discord Reaction
    if ctx.discord_client and rescue_request.discord_message_id:
        roleplay_cog = ctx.discord_client.get_cog('RoleplayCog')
        if roleplay_cog:
            asyncio.run_coroutine_threadsafe(
                roleplay_cog.add_reaction_to_rescue_message(rescue_request.discord_message_id, '👍'),
                ctx.discord_client.loop
            )
