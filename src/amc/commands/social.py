import asyncio
from amc.command_framework import registry, CommandContext
from amc.models import Character, Thank, Player
from amc.mod_server import send_system_message
from amc.game_server import get_players2
from datetime import timedelta
from django.db.models import F
from django.utils.translation import gettext as _, gettext_lazy

@registry.register("/thank", description=gettext_lazy("Thank another player to increase their social score"), category="Social")
async def cmd_thank(ctx: CommandContext, target_player_name: str):
    if target_player_name == ctx.character.name: return
    
    players = await get_players2(ctx.http_client)
    target_guid = next((p['character_guid'] for pid, p in players if p['name'].startswith(target_player_name)), None)
    
    if not target_guid:
        await ctx.reply(_("Player not found"))
        return

    try:
        target_char = await Character.objects.aget(guid=target_guid)
    except Character.DoesNotExist:
        await ctx.reply(_("Player not found in DB"))
        return
    # Check cooldown
    if await Thank.objects.filter(sender_character=ctx.character, recipient_character=target_char, timestamp__gte=ctx.timestamp - timedelta(hours=1)).aexists():
        await ctx.reply(_("Already thanked recently."))
        return

    await Thank.objects.acreate(sender_character=ctx.character, recipient_character=target_char, timestamp=ctx.timestamp)
    
    await Player.objects.filter(characters=target_char).aupdate(social_score=F('social_score')+1)
    
    asyncio.create_task(send_system_message(ctx.http_client_mod, _("Thank sent"), character_guid=ctx.character.guid))
    asyncio.create_task(send_system_message(ctx.http_client_mod, _("{sender} thanked you").format(sender=ctx.character.name), character_guid=str(target_char.guid)))
