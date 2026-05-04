import asyncio
import re
from amc.command_framework import registry, CommandContext
from amc.models import BotInvocationLog, SongRequestLog
from amc.mod_server import show_popup
from amc.player_tags import strip_all_tags, refresh_player_name
from django.conf import settings
from amc.game_server import get_player_info
from amc.auth import verify_player
from amc.utils import add_discord_verified_role
from django.utils.translation import gettext as _, gettext_lazy


@registry.register(
    "/help", description=gettext_lazy("Show this help message"), category="General"
)
async def cmd_help(ctx: CommandContext):
    # Group commands by category
    categories = {}
    featured_cmds = []
    is_admin = ctx.player_info.get("bIsAdmin", False) if ctx.player_info else False

    for cmd in registry.commands:
        cat = cmd.get("category", "General")

        if cat == "Admin" and not is_admin:
            continue

        if cat not in categories:
            categories[cat] = []
        categories[cat].append(cmd)

        # Collect featured commands
        if cmd.get("featured", False):
            featured_cmds.append(cmd)

    msg = _("<Title>Available Commands</> \n\n")

    # Show Featured section first
    if featured_cmds:
        msg += _("<Title>Featured</>\n<Secondary></>\n")
        for cmd in featured_cmds:
            name = cmd["name"]
            aliases = cmd.get("aliases", [name])
            desc = str(cmd.get("description", ""))

            shorthands = [a for a in aliases if a != name]
            shorthand_str = ""
            if shorthands:
                shorthand_str = "\n" + _(
                    "<Secondary>Shorthand: {shorthands}</>"
                ).format(shorthands=", ".join(shorthands))

            msg += f"<Highlight>{name}</> - {desc}{shorthand_str}\n<Secondary></>\n"
        msg += "\n"

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
            name = cmd["name"]
            aliases = cmd.get("aliases", [name])
            # Ensure description is translated using current context
            desc = str(cmd.get("description", ""))

            # Shorthands
            shorthands = [a for a in aliases if a != name]
            shorthand_str = ""
            if shorthands:
                shorthand_str = "\n" + _(
                    "<Secondary>Shorthand: {shorthands}</>"
                ).format(shorthands=", ".join(shorthands))

            msg += f"<Highlight>{name}</> - {desc}{shorthand_str}\n<Secondary></>\n"

    await ctx.reply(msg)
    await BotInvocationLog.objects.acreate(
        timestamp=ctx.timestamp, character=ctx.character, prompt="help"
    )


@registry.register(
    ["/credit", "/credits"],
    description=gettext_lazy(
        "List the awesome people who made this community possible"
    ),
    category="General",
)
async def cmd_credits(ctx: CommandContext):
    await ctx.reply(settings.CREDITS_TEXT)
    await BotInvocationLog.objects.acreate(
        timestamp=ctx.timestamp, character=ctx.character, prompt="credits"
    )


@registry.register(
    ["/coords", "/loc"],
    description=gettext_lazy("See your current coordinates"),
    category="General",
)
async def cmd_coords(ctx: CommandContext):
    player_info = await get_player_info(ctx.http_client, str(ctx.player.unique_id))
    if player_info and player_info.get("Location"):
        loc = player_info["Location"]
        await ctx.announce(
            f"{int(float(loc['X']))}, {int(float(loc['Y']))}, {int(float(loc['Z']))}"
        )


@registry.register(
    "/verify", description=gettext_lazy("Verify your account"), category="General"
)
async def cmd_verify(ctx: CommandContext, signed_message: str):
    try:
        discord_user_id = await verify_player(ctx.player, signed_message)

        if ctx.discord_client:
            asyncio.run_coroutine_threadsafe(
                add_discord_verified_role(
                    ctx.discord_client, discord_user_id, str(ctx.player.unique_id)
                ),
                ctx.discord_client.loop,
            )

        asyncio.create_task(
            show_popup(
                ctx.http_client_mod,
                "You are now verified!",
                character_guid=ctx.character.guid,
                player_id=str(ctx.player.unique_id),
            )
        )
    except Exception as e:
        asyncio.create_task(
            show_popup(
                ctx.http_client_mod,
                f"Failed to verify: {e}",
                character_guid=ctx.character.guid,
                player_id=str(ctx.player.unique_id),
            )
        )


@registry.register(
    "/rename", description=gettext_lazy("Rename your character"), category="General"
)
async def cmd_rename(ctx: CommandContext, name: str):
    if re.search(r"\[GOV\d*\]", name, re.IGNORECASE) and not ctx.character.is_gov_employee:
        await ctx.reply("This tag is reserved for government employees.")
        return

    clean_name = strip_all_tags(name)

    if len(clean_name) > 20 or "(" in clean_name:
        await ctx.reply("Invalid name")
        return

    ctx.character.name = clean_name
    await ctx.character.asave(update_fields=["name"])
    await refresh_player_name(ctx.character, ctx.http_client_mod)


@registry.register(
    "/bot",
    description=gettext_lazy("Ask the bot a question"),
    category="General",
    featured=True,
)
async def cmd_bot(ctx: CommandContext, prompt: str):
    await BotInvocationLog.objects.acreate(
        timestamp=ctx.timestamp, character=ctx.character, prompt=prompt
    )


@registry.register(
    "/tags",
    description=gettext_lazy("Explain player name tags"),
    category="General",
)
async def cmd_tags(ctx: CommandContext):
    await ctx.reply(
        _(
            "<Title>Player Tags</>\n\n"
            "Tags appear in brackets before your name, e.g. [RM***C2G3] Player\n\n"
            "<Highlight>[R]</> RP Mode — You are in roleplay mode\n"
            "<Highlight>[M]</> Modded Parts — Your current vehicle has custom/modded parts\n"
            "<Highlight>[P#]</> Police — On police duty (number = level)\n"
            "<Highlight>[*]</> Wanted — Wanted level (1–5 stars)\n"
            "<Highlight>[C#]</> Criminal — Active criminal record (number = level; hidden when on police duty)\n"
            "<Highlight>[G#]</> Government — Government employee (number = level)\n"
            "<Highlight>[DOT]</> DOT — Permanent tag for Department of Transport members\n\n"
            "<Secondary>Tags are automatically updated based on your status. Use /rp_mode to toggle RP, /police to toggle police duty, /workforgov to become a government employee.</>"
        )
    )


@registry.register(
    ["/song_request", "/songrequest"],
    description=gettext_lazy("Request a song for the radio"),
    category="General",
    featured=True,
)
async def cmd_song_request(ctx: CommandContext, song: str):
    await SongRequestLog.objects.acreate(
        timestamp=ctx.timestamp, character=ctx.character, song=song
    )
    if ctx.is_current_event:
        asyncio.create_task(
            show_popup(
                ctx.http_client_mod,
                "<Title>Your song is being downloaded</>\n\nThis usually takes 30-60 seconds.",
                character_guid=ctx.character.guid,
                player_id=str(ctx.player.unique_id),
            )
        )
    else:
        await ctx.reply("Song request received")
