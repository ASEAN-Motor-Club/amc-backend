import asyncio
import logging
import math
import re

from django.utils import timezone
from django.utils.translation import gettext as gettext, gettext_lazy

from amc.command_framework import registry, CommandContext
from amc.game_server import announce
from amc.models import (
    TeleportPoint,
    Confiscation,
    Wanted,
)
from amc.mod_server import (
    clear_suspect,
    force_exit_vehicle,
    get_player,
    send_system_message,
    show_popup,
    teleport_player,
    transfer_money,
)
from amc_finance.services import (
    record_treasury_confiscation_income,
)
from amc.pipeline.profit import on_player_profit
from amc.police import (
    get_active_police_characters,
    record_confiscation_for_level,
)
from datetime import timedelta
from amc.player_tags import refresh_player_name

logger = logging.getLogger("amc.commands.faction")

# 100 game units = 1 metre
ARREST_RADIUS_ON_FOOT = 3000  # 30m — cop on foot (consistent with auto-arrest)
ARREST_RADIUS_IN_VEHICLE = 2000  # 20m — cop in vehicle (consistent with auto-arrest)
SUSPECT_SPEED_LIMIT = 556  # ~5.56m/s ≈ 20km/h — suspects moving faster are immune
ARREST_POLL_COUNT = 3  # 3 polls × 1s = 3 seconds (consistent with auto-arrest)
ARREST_POLL_INTERVAL = 1  # seconds between polls
ARREST_COOLDOWN = 0  # seconds between arrests per cop

_LOC_RE = re.compile(r"X=(?P<x>[-\d.]+)\s+Y=(?P<y>[-\d.]+)\s+Z=(?P<z>[-\d.]+)")


def parse_location_string(loc_str: str) -> tuple[float, float, float]:
    """Parse 'X=-53918.590 Y=153629.920 Z=-20901.710' → (x, y, z)."""
    m = _LOC_RE.search(loc_str)
    if not m:
        raise ValueError(f"Cannot parse location: {loc_str}")
    return float(m["x"]), float(m["y"]), float(m["z"])


def _distance_3d(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _build_player_locations(
    players: list,
) -> dict[str, tuple[str, tuple[float, float, float], bool]]:
    """Build guid → (unique_id, (x,y,z), has_vehicle) mapping from game server player list."""
    result = {}
    for _uid, pdata in players:
        guid = pdata.get("character_guid")
        loc_str = pdata.get("location")
        if not guid or not loc_str:
            continue
        try:
            loc = parse_location_string(loc_str)
        except ValueError:
            continue
        has_vehicle = bool(pdata.get("vehicle"))
        result[guid] = (pdata["unique_id"], loc, has_vehicle)
    return result


async def execute_arrest(
    officer_character,
    targets: dict,
    target_chars: dict,
    http_client,
    http_client_mod,
    reason: str = "",
) -> tuple[list[str], int]:
    """Execute arrest: teleport to jail, confiscate money, announce.

    Args:
        officer_character: The arresting officer's Character model, or None for
            a system/automated arrest.  When None, no reward is paid to an
            officer and no officer-specific messages are sent.
        targets: guid -> (unique_id, location, has_vehicle) for each suspect.
        target_chars: guid -> Character model for each suspect.
        http_client: Game server HTTP client (for announcements).
        http_client_mod: Mod server HTTP client (for teleport, money, messages).
        reason: Human-readable reason for the arrest, shown to the arrested
            player.  Empty string for no reason display.

    Returns:
        (arrested_names, total_confiscated) tuple.
    """
    try:
        jail_tp = await TeleportPoint.objects.aget(name__iexact="jail")
        jail_location = {
            "X": jail_tp.location.x,
            "Y": jail_tp.location.y,
            "Z": jail_tp.location.z,
        }
    except TeleportPoint.DoesNotExist:
        raise ValueError("Jail teleport point not configured.")

    arrested_names = []
    total_confiscated = 0
    for guid, (crim_uid, crim_loc, has_vehicle) in targets.items():
        name = target_chars[guid].name if guid in target_chars else "Unknown"

        # --- Phase 1: Expire Wanted & confiscate amount (BEFORE teleport) ---
        # We must expire the Wanted record before teleporting to jail.
        # Otherwise the ServerTeleportCharacter event handler will see an
        # active Wanted and apply a second penalty.
        suspect_char = target_chars.get(guid)
        confiscated_amount = 0
        if suspect_char:
            wanted = None
            try:
                wanted = await Wanted.objects.aget(
                    character=suspect_char, expired_at__isnull=True
                )
            except Wanted.DoesNotExist:
                pass

            # Confiscation = the bounty (Wanted.amount) ONLY — set at wanted
            # creation to 10% of the criminal score, frozen for the chase
            # (freeman 2026-09-20). No wanted → jail-only arrest, no money,
            # no score change (§plan 8.3). Negative amount = wrongful wanted
            # compensation path, unchanged.
            bounty = 0
            if wanted:
                bounty = wanted.amount

                # Expire Wanted status BEFORE teleport
                wanted.wanted_remaining = 0
                wanted.expired_at = timezone.now()
                await wanted.asave(update_fields=["wanted_remaining", "expired_at"])
                # Teleport lock rides the invisible flag now, not the name.
                from amc.no_teleport import sync_no_teleport

                await sync_no_teleport(suspect_char, http_client_mod)

            confiscated_amount = bounty

            # Create a Confiscation record for the arrest
            await Confiscation.objects.acreate(
                character=suspect_char,
                officer=officer_character,  # None for system arrests
                cargo_key="Illicit",
                amount=confiscated_amount,
            )

            # The confiscated amount reduces the criminal score (freeman
            # 2026-09-20) — money and rap sheet bite identically. Only
            # positive bounties negate; negative (wrongful-wanted
            # compensation) amounts never inflate the score.
            if bounty > 0:
                await suspect_char.arefresh_from_db(fields=["criminal_score"])
                suspect_char.criminal_score = max(
                    0, suspect_char.criminal_score - bounty
                )
                await suspect_char.asave(update_fields=["criminal_score"])

            # Arrest clears costume-suspect state (same as the old record
            # clear): the player still wears the costume, but the flag drives
            # the overlay reapplication loop, so dropping it stops the GE
            # from being re-applied while jailed. The next equipment webhook
            # re-syncs the flag.
            suspect_char.wearing_costume = False
            suspect_char.costume_item_key = None
            await suspect_char.asave(
                update_fields=["wearing_costume", "costume_item_key"]
            )

            # Refresh display name now that wanted state is finalized
            if wanted:
                asyncio.create_task(
                    refresh_player_name(suspect_char, http_client_mod)
                )

            # Drop the in-game suspect GE proactively — both the wanted and the
            # costume-criminal paths are now cleared above, so the reconciliation
            # loop would eventually clear it on its next 10 s tick, but the
            # arrestee is being teleported to jail right now and we want the
            # blue overlay / Net_Suspects entry to disappear immediately.
            # Safe to call on non-suspects (no-op when no GE is active).
            if suspect_char.guid:
                try:
                    await clear_suspect(http_client_mod, suspect_char.guid)
                except Exception:
                    logger.warning(
                        "clear_suspect failed for %s after arrest",
                        suspect_char.name,
                    )

            if confiscated_amount > 0:
                # (The score negation above IS the criminal-side penalty;
                # the old criminal_laundered_total reduction is subsumed.)

                await transfer_money(
                    http_client_mod,
                    int(-confiscated_amount),
                    "Money Confiscated",
                    str(suspect_char.player_id),
                )

                await record_treasury_confiscation_income(
                    confiscated_amount, "Police Confiscation"
                )

                # --- Spread confiscation to all online police ---
                online_police = [
                    c
                    async for c in await get_active_police_characters()
                ]
                # Filter out AFK officers — they don't receive rewards
                active_police = []
                for officer in online_police:
                    player_data = await get_player(
                        http_client_mod, str(officer.player_id)
                    )
                    if player_data and player_data.get("bAFK"):
                        continue
                    active_police.append(officer)
                if active_police:
                    per_officer_money = max(
                        1, confiscated_amount // len(active_police)
                    )
                    for officer in active_police:
                        await record_confiscation_for_level(
                            officer,
                            confiscated_amount,
                            http_client=http_client,
                            session=http_client_mod,
                        )
                        await transfer_money(
                            http_client_mod,
                            int(per_officer_money),
                            "Confiscation Reward",
                            str(officer.player_id),
                        )
                        await on_player_profit(
                            officer,
                            0,
                            per_officer_money,
                            http_client_mod,
                            http_client,
                        )
                        await send_system_message(
                            http_client_mod,
                            gettext(
                                "Confiscated ${total:,} in illegal earnings from {name}. Your share: ${share:,}."
                            ).format(
                                total=confiscated_amount,
                                name=name,
                                share=per_officer_money,
                            ),
                            character_guid=officer.guid,
                        )

                await send_system_message(
                    http_client_mod,
                    gettext(
                        "Police confiscated ${amount:,} in illegal earnings from your account."
                    ).format(amount=confiscated_amount),
                    character_guid=suspect_char.guid,
                )

            # (no financial action for zero-amount arrests)

        total_confiscated += confiscated_amount


        # --- Phase 2: Physical arrest (teleport to jail) ---
        # Always attempt to exit vehicle — snapshot may be stale
        try:
            await force_exit_vehicle(http_client_mod, guid)
            await asyncio.sleep(1.5)
        except Exception:
            pass

        # Teleport to jail — try without vehicle first, fallback to with-vehicle
        try:
            await teleport_player(
                http_client_mod,
                crim_uid,
                jail_location,
                no_vehicles=True,
                force=True,
            )
        except Exception:
            # Player still in vehicle — teleport with vehicle as fallback
            try:
                await teleport_player(
                    http_client_mod,
                    crim_uid,
                    jail_location,
                    no_vehicles=False,
                    force=True,
                )
            except Exception:
                continue  # teleport failed but confiscation already recorded

        # Popup notification
        popup_msg = "You have been arrested!"
        if reason:
            popup_msg += f"\n\nReason: {reason}"
        await show_popup(
            http_client_mod,
            popup_msg,
            player_id=crim_uid,
        )

        # Mark character as jailed so monitor_locations enforces jail perimeter
        if suspect_char:
            suspect_char.jailed_until = timezone.now() + timedelta(seconds=60)
            await suspect_char.asave(update_fields=["jailed_until"])

        arrested_names.append(name)

    return arrested_names, total_confiscated


async def perform_arrest(
    officer_character,
    targets: dict,
    target_chars: dict,
    http_client,
    http_client_mod,
    officer_message_format: str = "{names} arrested and sent to jail.",
    reason: str = "",
) -> tuple[list[str], int]:
    """Execute arrest and send standard officer notification + server announcement.

    Wraps :func:`execute_arrest` with uniform post-arrest messaging so callers
    do not duplicate notification logic.

    Raises:
        ValueError: If jail teleport point is not configured.
    """
    arrested_names, total_confiscated = await execute_arrest(
        officer_character=officer_character,
        targets=targets,
        target_chars=target_chars,
        http_client=http_client,
        http_client_mod=http_client_mod,
        reason=reason,
    )

    if arrested_names and officer_character:
        names_arrested = ", ".join(arrested_names)
        await send_system_message(
            http_client_mod,
            officer_message_format.format(names=names_arrested),
            character_guid=officer_character.guid,
        )
        if total_confiscated > 0:
            await announce(
                f"{names_arrested} arrested by {officer_character.name}! "
                f"${total_confiscated:,} confiscated.",
                http_client,
            )
        else:
            await announce(
                f"{names_arrested} arrested by {officer_character.name}!",
                http_client,
            )

    return arrested_names, total_confiscated


@registry.register(
    ["/arrest", "/a"],
    description=gettext_lazy("Arrest nearby suspects (Cops only) — DEPRECATED"),
    category="Faction",
)
async def cmd_arrest(ctx: CommandContext):
    """DEPRECATED: Manual /arrest has been removed. Auto-arrest is now the only mechanism."""
    await ctx.reply(
        gettext(
            "<Title>Command Deprecated</>\n\n"
            "/arrest is no longer available. Suspects are now arrested automatically "
            "when they log out near police, enter restricted zones, or use modded vehicles."
        )
    )
