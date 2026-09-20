import math

from amc.command_framework import registry, CommandContext
from amc.game_server import get_players
from amc.models import Character, Wanted
from amc.mod_server import (
    get_player,
    get_player_customization,
    make_suspect,
    send_system_message,
    show_popup,
    teleport_player,
)
from amc.police import (
    activate_police,
    deactivate_police,
    is_police,
    POLICE_STATIONS,
)
from amc.criminals import create_or_refresh_wanted
from amc.utils import fuzzy_find_player, with_verification_code
from django.conf import settings
from django.utils.translation import gettext as _, gettext_lazy

from amc.commands.faction import parse_location_string

SETWANTED_MIN_DISTANCE = 100_000  # 1km = 100,000 units (1m = 100 units)


def _distance_3d(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


@registry.register(
    "/police",
    description=gettext_lazy("Toggle police duty on/off"),
    category="Faction",
    featured=True,
)
async def cmd_police(ctx: CommandContext, verification_code: str = ""):
    active = await is_police(ctx.character)

    if active:
        await deactivate_police(ctx.character, ctx.http_client_mod)
        await send_system_message(
            ctx.http_client_mod,
            _("You are now off duty."),
            character_guid=ctx.character.guid,
        )
        await send_system_message(
            ctx.http_client_mod,
            _("You are now off police duty."),
            character_guid=ctx.character.guid,
        )
    else:
        # Wanted criminals may not become police
        has_wanted = await Wanted.objects.filter(
            character=ctx.character, expired_at__isnull=True
        ).aexists()
        if has_wanted:
            await send_system_message(
                ctx.http_client_mod,
                _("You cannot go on police duty while you are wanted."),
                character_guid=ctx.character.guid,
            )
            return

        # Police costume check — must be wearing a police uniform
        customization = await get_player_customization(
            ctx.http_client_mod, ctx.character.guid
        )
        costume_key = customization.get("Costume") if customization else None
        if costume_key not in settings.POLICE_COSTUMES:
            await show_popup(
                ctx.http_client_mod,
                _(
                    "<Title>Cannot Go On Duty</>\n\n"
                    "You must equip the police uniform before going on duty."
                ),
                character_guid=ctx.character.guid,
            )
            return

        # Fetch live player list for location and vehicle check
        players = await get_players(ctx.http_client)
        pdata = None
        for uid, p in players:
            if str(uid) == str(ctx.player.unique_id):
                pdata = p
                break

        # Vehicle check
        if pdata and bool(pdata.get("vehicle")):
            await ctx.reply(
                _("<Title>Cannot Go On Duty</>\n\nPlease exit the vehicle first.")
            )
            return

        # Criminal score gate — a positive score does not hard-block duty, but
        # going on duty requires a verified reset to 0. The gate sits LAST so
        # the wipe can only fire when every other check has already passed.
        if ctx.character.criminal_score > 0:
            code_expected, verified = with_verification_code(
                (ctx.character.id, ctx.character.criminal_score),
                verification_code,
            )
            if not verified:
                await ctx.reply(
                    _(
                        "<Title>Criminal Score</>\n\n"
                        "Your criminal score: <Money>{score:,}</>\n"
                        "Going on police duty will <Warning>permanently reset</> "
                        "your criminal score to <Money>0</>.\n"
                        "To confirm, type: <Highlight>/police {code}</>"
                    ).format(
                        score=ctx.character.criminal_score,
                        code=code_expected.upper(),
                    )
                )
                return
            ctx.character.criminal_score = 0
            await ctx.character.asave(update_fields=["criminal_score"])
            await send_system_message(
                ctx.http_client_mod,
                _("Your criminal score has been reset to 0."),
                character_guid=ctx.character.guid,
            )

        # Parse location and teleport to nearest police station
        if pdata and pdata.get("location"):
            try:
                loc = parse_location_string(pdata["location"])

                suspect_locations = []
                async for wanted in Wanted.objects.filter(
                    expired_at__isnull=True,
                    wanted_remaining__gt=0,
                ):
                    entry = next(
                        (
                            p
                            for pid, p in players
                            if p.get("character_guid") == str(wanted.character_id)
                        ),
                        None,
                    )
                    if entry and entry.get("location"):
                        try:
                            suspect_locations.append(
                                parse_location_string(entry["location"])
                            )
                        except ValueError:
                            pass

                nearest = None
                min_dist = float("inf")
                for name, tx, ty, tz in POLICE_STATIONS:
                    if any(
                        _distance_3d(sloc, (tx, ty, tz)) < SETWANTED_MIN_DISTANCE
                        for sloc in suspect_locations
                    ):
                        continue
                    dist = _distance_3d(loc, (tx, ty, tz))
                    if dist < min_dist:
                        min_dist = dist
                        nearest = (name, tx, ty, tz)
                if nearest:
                    station_name, tx, ty, tz = nearest
                    await teleport_player(
                        ctx.http_client_mod,
                        str(ctx.player.unique_id),
                        {"X": tx, "Y": ty, "Z": tz},
                        no_vehicles=True,
                    )
            except ValueError:
                pass

        await activate_police(ctx.character, ctx.http_client_mod)
        await ctx.announce(f"{ctx.character.name} is now on police duty!")


@registry.register(
    ["/setwanted", '/sw'],
    description=gettext_lazy("Set a player as wanted (admin only)"),
    category="Admin",
)
async def cmd_setwanted(ctx: CommandContext, target_player_name: str):
    # Only game admins can use this command
    if not ctx.player_info or not ctx.player_info.get("bIsAdmin"):
        return

    # Find the target player online
    players = await get_players(ctx.http_client)
    target_pid = fuzzy_find_player(players, target_player_name)

    if not target_pid:
        await ctx.reply(
            _(
                "<Title>Player not found</>\n\n"
                "Please make sure you typed the name correctly."
            )
        )
        return

    # Cannot set wanted on yourself
    if str(target_pid) == str(ctx.player.unique_id):
        await ctx.reply(_("You cannot set yourself as wanted."))
        return

    # Resolve the target character from the game data
    target_player_data = next(
        (p for pid, p in players if str(pid) == str(target_pid)), None
    )
    if not target_player_data:
        return

    try:
        target_character = await Character.objects.aget(
            guid=target_player_data["character_guid"]
        )
    except Character.DoesNotExist:
        await ctx.reply(_("Character not found in database."))
        return

    # Guard: target must not already be wanted
    already_wanted = await Wanted.objects.filter(
        character=target_character, expired_at__isnull=True
    ).aexists()
    if already_wanted:
        await ctx.reply(
            _("<Title>Already Wanted</>\n\n{name} is already wanted.").format(
                name=target_character.name
            )
        )
        return

    # Require a readable target location — the flag is anchored to a live
    # in-game player. Officers near the target are NOT relocated: on-duty
    # officers are teleport-locked server-side via the R tag, so a
    # relocate-to-station teleport can no longer be enforced. Proximity at
    # flag time is organic — officers can only be close by having driven
    # there.
    target_location_str = target_player_data.get("location")
    if not target_location_str:
        await ctx.reply(
            _(
                "<Title>Location Unknown</>\n\nCannot determine {name}'s location."
            ).format(name=target_character.name)
        )
        return

    try:
        parse_location_string(target_location_str)
    except ValueError:
        await ctx.reply(
            _(
                "<Title>Location Unknown</>\n\nCannot determine {name}'s location."
            ).format(name=target_character.name)
        )
        return

    # AFK check: police may not set AFK players as wanted
    target_live = await get_player(ctx.http_client, str(target_pid), force_refresh=True)
    if target_live and target_live.get("bAFK"):
        await ctx.reply(
            _(
                "<Title>Player AFK</>\n\n"
                "{name} is currently AFK and cannot be set as wanted."
            ).format(name=target_character.name)
        )
        return

    # Innocence check: criminal_score is the single source of truth.
    # A score of 0 means no illicit activity on the ledger.
    has_criminal_score = target_character.criminal_score > 0

    if has_criminal_score:
        # Legitimate wanted — standard minimum bounty applied inside create_or_refresh_wanted
        bounty_amount = 0
        warning_note = ""
    else:
        # Innocent civilian — no financial penalty, just a warning note
        bounty_amount = 0
        warning_note = " WARNING: No recent illicit activity detected."

    # Create or refresh the wanted record
    await create_or_refresh_wanted(
        target_character,
        ctx.http_client_mod,
        amount=bounty_amount,
        set_by=ctx.character,
    )

    # Flag the target as a suspect in-game
    await make_suspect(ctx.http_client_mod, target_character.guid)

    await ctx.reply(
        _("<Title>Wanted Set</>\n\n{name} is now wanted!{note}").format(
            name=target_character.name, note=warning_note
        )
    )
    await ctx.announce(
        f"{target_character.name} has been marked as wanted by {ctx.character.name}!"
    )


