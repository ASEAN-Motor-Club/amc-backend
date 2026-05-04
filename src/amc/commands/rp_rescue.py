import asyncio
from amc.command_framework import registry, CommandContext
from amc.models import RescueRequest, RPSession
from amc.config import RP_MIN_DURATION_HOURS
from django.contrib.gis.geos import Point
from amc.mod_server import (
    get_players as get_players_mod,
    get_player_last_vehicle,
    send_system_message,
    despawn_player_vehicle,
)
from amc.game_server import get_player_info
from amc.player_tags import refresh_player_name
from amc.utils import with_verification_code
from amc.vehicles import format_vehicle_name
from django.utils import timezone
from datetime import timedelta
from django.conf import settings
from django.utils.translation import gettext as _, gettext_lazy


@registry.register(
    ["/rp_mode", "/rp"],
    description=gettext_lazy("Toggle RP mode on your character"),
    category="RP & Rescue",
    featured=True,
)  # type: ignore
async def cmd_rp_mode(ctx: CommandContext, verification_code: str = ""):
    if ctx.character.rp_mode:
        # --- Disabling RP mode ---
        session = await RPSession.aget_active(ctx.character)
        if session:
            elapsed = timezone.now() - session.created_at
            min_duration = timedelta(hours=RP_MIN_DURATION_HOURS)
            if elapsed < min_duration:
                remaining = min_duration - elapsed
                minutes = int(remaining.total_seconds() // 60)
                await ctx.reply(
                    _("<Title>RP Mode</>\n"
                      "You must remain in RP mode for at least {hours}h. "
                      "Time remaining: {minutes}min."
                    ).format(hours=RP_MIN_DURATION_HOURS, minutes=minutes)
                )
                return

        code_expected, verified = with_verification_code(
            ("rp_mode_off", ctx.character.id), verification_code
        )
        if not verified:
            await ctx.reply(
                _(
                    "<Title>Disable RP Mode</>\n"
                    "Are you sure you want to disable RP mode?\n\n"
                    "To confirm, type: <Highlight>/rp {code}</>"
                ).format(code=code_expected.upper())
            )
            return

        if session:
            session.expires_at = timezone.now()
            await session.asave(update_fields=["expires_at"])

        ctx.character.rp_mode = False
        await ctx.character.asave(update_fields=["rp_mode"])
        asyncio.create_task(refresh_player_name(ctx.character, ctx.http_client_mod))
        await ctx.reply(_("<Title>RP Mode Disabled</>"))
    else:
        # --- Enabling RP mode ---
        code_expected, verified = with_verification_code(
            ("rp_mode_on", ctx.character.id), verification_code
        )
        if not verified:
            await ctx.reply(
                _(
                    "<Title>Enable RP Mode</>\n"
                    "RP mode enforces realistic driving restrictions. "
                    "You must stay in RP mode for at least <Highlight>{hours} hour(s)</>.\n\n"
                    "<Warning>When RP mode is ON:</>\n"
                    "- Roadside recovery is <Warning>disabled</> (use /rescue for help)\n"
                    "- Teleporting is <Warning>disabled</>\n"
                    "- Autopilot / AI driving is <Warning>disabled</>\n"
                    "- Your personal vehicles will be <Warning>despawned</>\n"
                    "- You will receive the <Highlight>[R]</> tag in your name\n\n"
                    "To confirm, type: <Highlight>/rp {code}</>"
                ).format(hours=RP_MIN_DURATION_HOURS, code=code_expected.upper())
            )
            return

        await RPSession.objects.acreate(character=ctx.character)
        ctx.character.rp_mode = True
        await ctx.character.asave(update_fields=["rp_mode"])
        asyncio.create_task(refresh_player_name(ctx.character, ctx.http_client_mod))
        await despawn_player_vehicle(ctx.http_client_mod, str(ctx.character.guid), category="all")
        await ctx.reply(
            _("<EffectGood>RP Mode Enabled</>\n"
              "You may no longer use roadside recovery to reset vehicles. "
              "Call /rescue if you need help.\n"
              "Minimum RP duration: {hours}h."
            ).format(hours=RP_MIN_DURATION_HOURS)
        )


@registry.register(
    "/rescue",
    description=gettext_lazy("Calls for rescue service"),
    category="RP & Rescue",
    featured=True,
)  # type: ignore
async def cmd_rescue(ctx: CommandContext, message: str = ""):
    if await RescueRequest.objects.filter(
        character=ctx.character, timestamp__gte=timezone.now() - timedelta(minutes=5)
    ).aexists():
        await ctx.reply(_("You have requested a rescue less than 5 minutes ago"))
        return

    # 1. Notify In-Game Rescuers
    players = await get_players_mod(ctx.http_client_mod)
    try:
        last_vehicle = await get_player_last_vehicle(
            ctx.http_client_mod, str(ctx.character.guid)
        )
        vehicle = last_vehicle.get("vehicle")
    except Exception:
        vehicle = None
    vehicle_name = format_vehicle_name(vehicle["fullName"]) if vehicle else _("Vehicles")

    sent = False
    if players:
        for p in players:
            if "[ARWRS]" in p.get("PlayerName", "") or "[DOT]" in p.get(
                "PlayerName", ""
            ):
                asyncio.create_task(
                    send_system_message(
                        ctx.http_client_mod,
                        _("{name} needs help!").format(name=ctx.character.name),
                        character_guid=p.get("CharacterGuid"),
                    )
                )
                sent = True

    # 2. Create DB Entry
    location = None
    player_info = await get_player_info(ctx.http_client, str(ctx.player.unique_id))
    if player_info and player_info.get("Location"):
        loc = player_info["Location"]
        location = Point(loc["X"], loc["Y"], loc.get("Z", 0), srid=0)

    rescue_request = await RescueRequest.objects.acreate(
        character=ctx.character, message=message, location=location, last_reminded_at=timezone.now()
    )

    if ctx.is_current_event:
        await ctx.announce(
            _(
                "{name} needs a rescue! {vehicle_name}. Respond with /respond {request_id}"
            ).format(
                name=ctx.character.name,
                vehicle_name=vehicle_name,
                request_id=rescue_request.id,
            )
        )
        await ctx.reply(
            _("<EffectGood>Request Sent>\n")
            + (
                _("Help is on the way.")
                if sent
                else _("Rescuers offline, notified Discord.")
            )
        )

    # 3. Discord Notification
    if ctx.discord_client:

        async def send_discord():
            from amc.utils import forward_to_discord

            msg = await forward_to_discord(
                ctx.discord_client,
                settings.DISCORD_RESCUE_CHANNEL_ID,
                _("@here **{name}** requested rescue.\nMsg: {message}").format(
                    name=ctx.character.name, message=message
                ),
                escape_mentions=False,
                silent=True,
            )
            if msg:
                rescue_request.discord_message_id = msg.id
                await rescue_request.asave()

        asyncio.run_coroutine_threadsafe(send_discord(), ctx.discord_client.loop)


@registry.register(
    "/respond",
    description=gettext_lazy("Respond to a rescue request"),
    category="RP & Rescue",
)
async def cmd_respond(ctx: CommandContext, rescue_id: int):
    try:
        rescue_request = await RescueRequest.objects.select_related("character").aget(
            pk=rescue_id
        )
    except RescueRequest.DoesNotExist:
        try:
            rescue_request = await RescueRequest.objects.select_related(
                "character"
            ).aget(timestamp__gte=timezone.now() - timedelta(minutes=5))
        except Exception:
            await ctx.reply(_("Invalid or expired rescue request."))
            return

    await rescue_request.responders.aadd(ctx.player)

    await ctx.announce(
        _("{responder} is responding to {requester}'s rescue request!").format(
            responder=ctx.character.name, requester=rescue_request.character.name
        )
    )

    await ctx.reply(
        _(
            "<Title>Rescue Response</>\n"
            "You are responding to {requester}'s rescue!\n\n"
            "<EffectGood>Teleport Enabled</>\n"
            "Use <Highlight>/tp</> with your custom destination marker "
            "to teleport within 10,000 units of {requester} for the next 10 minutes."
        ).format(requester=rescue_request.character.name)
    )

    # Discord Reaction
    if ctx.discord_client and rescue_request.discord_message_id:
        roleplay_cog = ctx.discord_client.get_cog("RoleplayCog")
        if roleplay_cog:
            asyncio.run_coroutine_threadsafe(
                roleplay_cog.add_reaction_to_rescue_message(
                    rescue_request.discord_message_id, "👍"
                ),
                ctx.discord_client.loop,
            )
