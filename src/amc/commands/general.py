import asyncio
from amc.command_framework import registry, CommandContext
from amc.models import BotInvocationLog, CharacterLocation, SongRequestLog
from amc.mod_server import set_character_name, show_popup
from django.conf import settings
from django.db.models import Q
from datetime import timedelta
from amc.locations import gwangjin_shortcut, migeum_shortcut
from amc.mod_server import get_player
from amc.auth import verify_player
from amc.utils import add_discord_verified_role
from django.utils.translation import gettext as _, gettext_lazy

@registry.register("/help", description=gettext_lazy("Show this help message"), category="General")
async def cmd_help(ctx: CommandContext):
    # Group commands by category
    categories = {}
    for cmd in registry.commands:
        cat = cmd.get('category', 'General')
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(cmd)
    
    msg = _("<Title>Available Commands</>\n\n")
    
    # Sort categories: General first, then alphabetical
    cat_names = sorted(categories.keys())
    if "General" in cat_names:
        cat_names.remove("General")
        cat_names.insert(0, "General")
        
    for cat in cat_names:
        if cat != "General":
            # Translate category name if needed, but categories are usually IDs. 
            # We can try to translate them or assume they are capitalized keys.
            msg += _("<Title>{cat}</>\n<Secondary></>\n").format(cat=cat)
            
        for cmd in categories[cat]:
            name = cmd['name']
            aliases = cmd.get('aliases', [name])
            # Ensure description is translated using current context
            desc = str(cmd.get('description', ''))
            
            # Shorthands
            shorthands = [a for a in aliases if a != name]
            shorthand_str = ""
            if shorthands:
                shorthand_str = "\n" + _("<Secondary>Shorthand: {shorthands}</>").format(shorthands=', '.join(shorthands))
            
            msg += f"<Highlight>{name}</> - {desc}{shorthand_str}\n<Secondary></>\n"
            
    await ctx.reply(msg)
    await BotInvocationLog.objects.acreate(
        timestamp=ctx.timestamp, character=ctx.character, prompt="help"
    )

@registry.register(["/credit", "/credits"], description=gettext_lazy("List the awesome people who made this community possible"), category="General")
async def cmd_credits(ctx: CommandContext):
    await ctx.reply(settings.CREDITS_TEXT)
    await BotInvocationLog.objects.acreate(
        timestamp=ctx.timestamp, character=ctx.character, prompt="credits"
    )

@registry.register(["/coords", "/loc"], description=gettext_lazy("See your current coordinates"), category="General")
async def cmd_coords(ctx: CommandContext):
    player_info = await get_player(ctx.http_client_mod, str(ctx.player.unique_id))
    if player_info:
        loc = player_info['Location']
        await ctx.announce(f"{int(float(loc['X']))}, {int(float(loc['Y']))}, {int(float(loc['Z']))}")

# @registry.register("/shortcutcheck", description=gettext_lazy("Check if you are inside a forbidden shortcut zone"), category="General")
async def cmd_shortcutcheck(ctx: CommandContext):
    in_shortcut = await CharacterLocation.objects.filter(
        Q(location__coveredby=gwangjin_shortcut) | Q(location__coveredby=migeum_shortcut),
        character=ctx.character,
        timestamp__gte=ctx.timestamp - timedelta(seconds=5),
    ).aexists()
    used_shortcut = await CharacterLocation.objects.filter(
        character=ctx.character,
        location__coveredby=gwangjin_shortcut,
        timestamp__gte=ctx.timestamp - timedelta(hours=1),
    ).aexists()
    
    msg = "<Title>Gwangjin Shortcut Status</>\n"
    msg += "<Warning>You are inside the forbidden zone</>\n" if in_shortcut else "<EffectGood>You are outside the forbidden zone</>\n"
    msg += "<Warning>You were detected inside the forbidden zone in the last hour</>\n" if used_shortcut else "<EffectGood>You have not been inside the forbidden zone for the last hour</>\n"
    
    await ctx.reply(msg)

@registry.register("/verify", description=gettext_lazy("Verify your account"), category="General")
async def cmd_verify(ctx: CommandContext, signed_message: str):
    try:
        discord_user_id = await verify_player(ctx.player, signed_message)
        
        if ctx.discord_client:
            asyncio.run_coroutine_threadsafe(
                add_discord_verified_role(
                    ctx.discord_client,
                    discord_user_id,
                    str(ctx.player.unique_id)
                ),
                ctx.discord_client.loop
            )
        
        asyncio.create_task(show_popup(ctx.http_client_mod, "You are now verified!", character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id)))
    except Exception as e:
        asyncio.create_task(show_popup(ctx.http_client_mod, f"Failed to verify: {e}", character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id)))

@registry.register("/rename", description=gettext_lazy("Rename your character"), category="General")
async def cmd_rename(ctx: CommandContext, name: str):
    if len(name) > 20 or '(' in name:
        await ctx.reply("Invalid name")
        return
    # RP Logic
    ctx.character.custom_name = name
    await ctx.character.asave()
    await set_character_name(ctx.http_client_mod, ctx.character.guid, name)

@registry.register("/bot", description=gettext_lazy("Ask the bot a question"), category="General")
async def cmd_bot(ctx: CommandContext, prompt: str):
    await BotInvocationLog.objects.acreate(timestamp=ctx.timestamp, character=ctx.character, prompt=prompt)

@registry.register(["/song.request", "/song_request"], description=gettext_lazy("Request a song for the radio"), category="General")
async def cmd_song_request(ctx: CommandContext, song: str):
    await SongRequestLog.objects.acreate(timestamp=ctx.timestamp, character=ctx.character, song=song)
    if ctx.is_current_event:
         asyncio.create_task(show_popup(ctx.http_client_mod, "<Title>Your song is being downloaded</>\n\nThis usually takes 30-60 seconds.", character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id)))
    else:
         await ctx.reply("Song request received")
