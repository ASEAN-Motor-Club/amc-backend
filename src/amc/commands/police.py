import math

from amc.command_framework import registry, CommandContext
from amc.game_server import get_players
from amc.models import Character, Wanted
from amc.mod_server import (
    despawn_player_vehicle,
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
    get_active_police_characters,
    is_police,
    POLICE_STATIONS,
)
from amc.utils import game_units_to_metres, compass_direction
from amc.criminals import create_or_refresh_wanted
from amc.utils import fuzzy_find_player
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _, gettext_lazy

from amc.commands.faction import parse_location_string, _build_player_locations

SETWANTED_COOLDOWN = timezone.timedelta(minutes=settings.SETWANTED_COOLDOWN_MINUTES)
SETWANTED_MIN_DISTANCE = 100_000  # 1km = 100,000 units (1m = 100 units)


def _distance_3d(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


@registry.register(
    "/police",
    description=gettext_lazy("Toggle police duty on/off"),
    category="Faction",
    featured=True,
)
async def cmd_police(ctx: CommandContext):
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

        # Active criminal record blocks police duty
        from amc.models import CriminalRecord

        has_active_record = await CriminalRecord.objects.filter(
            character=ctx.character, cleared_at__isnull=True
        ).aexists()
        if has_active_record:
            await send_system_message(
                ctx.http_client_mod,
                _(
                    "You cannot go on police duty while you have an active criminal record."
                ),
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
    "/setwanted",
    description=gettext_lazy("Set a player as wanted (police only)"),
    category="Faction",
)
async def cmd_setwanted(ctx: CommandContext, target_player_name: str):
    from amc.models import CriminalRecord

    # Only on-duty police can use this command
    if not await is_police(ctx.character):
        await ctx.reply(_("You must be on police duty to use this command."))
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

    # Cooldown: 1 hour since last Wanted expiry
    last_expired = (
        await Wanted.objects.filter(
            character=target_character, expired_at__isnull=False
        )
        .order_by("-expired_at")
        .afirst()
    )
    if last_expired:
        cooldown_end = last_expired.expired_at + SETWANTED_COOLDOWN
        now = timezone.now()
        if now < cooldown_end:
            remaining = cooldown_end - now
            remaining_mins = int(remaining.total_seconds() / 60)
            remaining_secs = int(remaining.total_seconds()) % 60
            countdown_msg = _(
                "<Title>Cooldown Active</>\n\n"
                "You can set {name} as wanted again in "
                "{mins}m {secs}s."
            ).format(
                name=target_character.name, mins=remaining_mins, secs=remaining_secs
            )
            await show_popup(
                ctx.http_client_mod,
                countdown_msg,
                character_guid=ctx.character.guid,
            )
            return

    # Distance check: target must be at least 1km away from any police officer.
    # Officers within range are despawned and teleported to the nearest station outside the radius.
    target_location_str = target_player_data.get("location")
    if not target_location_str:
        await ctx.reply(
            _(
                "<Title>Location Unknown</>\n\nCannot determine {name}'s location."
            ).format(name=target_character.name)
        )
        return

    try:
        target_loc = parse_location_string(target_location_str)
    except ValueError:
        await ctx.reply(
            _(
                "<Title>Location Unknown</>\n\nCannot determine {name}'s location."
            ).format(name=target_character.name)
        )
        return

    officers_to_move = []
    police_chars = await get_active_police_characters()
    async for police_char in police_chars:
        police_player_data = next(
            (
                p
                for pid, p in players
                if p.get("character_guid") == str(police_char.guid)
            ),
            None,
        )
        if not police_player_data:
            continue
        police_location_str = police_player_data.get("location")
        if not police_location_str:
            continue
        try:
            police_loc = parse_location_string(police_location_str)
        except ValueError:
            continue

        dist = _distance_3d(target_loc, police_loc)
        if dist < SETWANTED_MIN_DISTANCE:
            officers_to_move.append((police_char, police_player_data, police_loc))

    for police_char, police_player_data, police_loc in officers_to_move:
        police_uid = police_player_data.get("unique_id")
        if not police_uid:
            continue

        try:
            await despawn_player_vehicle(
                ctx.http_client_mod,
                str(police_char.guid),
                category="current",
            )
        except Exception:
            pass  # Best effort — proceed even if despawn fails

        # Find the police station closest to the officer that is outside the 2km radius from the suspect
        nearest_station = None
        min_officer_dist = float("inf")
        for name, tx, ty, tz in POLICE_STATIONS:
            station_dist_from_suspect = _distance_3d(target_loc, (tx, ty, tz))
            if station_dist_from_suspect >= SETWANTED_MIN_DISTANCE:
                officer_dist = _distance_3d(police_loc, (tx, ty, tz))
                if officer_dist < min_officer_dist:
                    min_officer_dist = officer_dist
                    nearest_station = (name, tx, ty, tz)

        if nearest_station:
            _station_name, tx, ty, tz = nearest_station
            try:
                await teleport_player(
                    ctx.http_client_mod,
                    str(police_uid),
                    {"X": tx, "Y": ty, "Z": tz},
                    no_vehicles=True,
                )
            except Exception:
                pass  # Best effort — proceed even if teleport fails

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

    # Innocence check: CriminalRecord is the single source of truth.
    # A NULL cleared_at means the character has an active criminal record.
    has_criminal_record = await CriminalRecord.objects.filter(
        character=target_character, cleared_at__isnull=True
    ).aexists()

    if has_criminal_record:
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


@registry.register(
    ["/suspects", "/s"],
    description=gettext_lazy(
        "List online wanted suspects with distance and bearing (police only)"
    ),
    category="Faction",
    featured=True,
)
async def cmd_suspects(ctx: CommandContext):
    if not await is_police(ctx.character):
        await ctx.reply(_("You must be on police duty to use this command."))
        return

    players = await get_players(ctx.http_client)
    locations = _build_player_locations(players) if players else {}

    officer_entry = locations.get(str(ctx.character.guid))
    if not officer_entry:
        await ctx.reply(_("Cannot determine your location."))
        return

    _officer_name, officer_loc, _officer_vehicle = officer_entry
    officer_x, officer_y, officer_z = officer_loc

    suspect_entries = []
    async for wanted in Wanted.objects.filter(
        expired_at__isnull=True,
        wanted_remaining__gt=0,
    ).select_related("character"):
        guid = wanted.character.guid
        if not guid or guid == str(ctx.character.guid):
            continue
        entry = locations.get(guid)
        if not entry:
            continue
        _suspect_name, suspect_loc, _suspect_vehicle = entry
        suspect_x, suspect_y, suspect_z = suspect_loc
        dx = suspect_x - officer_x
        dy = suspect_y - officer_y
        dist = _distance_3d(officer_loc, suspect_loc)
        direction = compass_direction(dx, dy)
        metres = game_units_to_metres(dist)
        suspect_entries.append((dist, wanted.character.name, metres, direction))

    if not suspect_entries:
        await ctx.reply(
            _("<Title>No Suspects</>\n\nNo wanted suspects are currently online.")
        )
        return

    suspect_entries.sort(key=lambda x: x[0])
    lines = ["<Title>Suspects</>", ""]
    for _entry_dist, name, metres, direction in suspect_entries:
        if metres < 1000:
            dist_str = f"{metres}m"
        else:
            dist_str = f"{metres / 1000:.1f}km"
        lines.append(f"{name} — {dist_str} {direction}")

    await ctx.reply("\n".join(lines))
