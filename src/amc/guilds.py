import logging

from django.utils import timezone

from amc.enums import VehicleKey, VehicleKeyByLabel
from amc.models import (
    Character,
    GuildCharacter,
    GuildCharacterAchievement,
    GuildSession,
    GuildVehicle,
    Player,
)
from amc.mod_server import get_decal, get_player_last_vehicle_parts, set_decal, show_popup
from amc.player_tags import refresh_player_name
from amc.special_cargo import ILLICIT_CARGO_KEYS

logger = logging.getLogger("amc.guilds")


async def _find_matching_guild_vehicle(
    vehicle_name: str, character_guid: str, http_client_mod
) -> GuildVehicle | None:
    vehicle_key = VehicleKeyByLabel.get(vehicle_name) or vehicle_name
    if vehicle_key not in VehicleKey.values:
        return None

    candidates = [
        gv
        async for gv in GuildVehicle.objects.filter(vehicle_key=vehicle_key)
        .select_related("guild", "decal")
        .prefetch_related("parts")
    ]
    if not candidates:
        return None

    has_any_parts = any(gv.parts.all() for gv in candidates)
    if not has_any_parts:
        return candidates[0]

    try:
        parts_data = await get_player_last_vehicle_parts(
            http_client_mod, character_guid, complete=False
        )
    except Exception as e:
        logger.error(f"Failed to fetch vehicle parts for guild check ({character_guid}): {e}")
        return None

    player_part_keys = {p["Key"] for p in parts_data.get("parts", [])}

    fallback_no_parts = None
    for gv in candidates:
        required = list(gv.parts.all())
        if not required:
            if fallback_no_parts is None:
                fallback_no_parts = gv
            continue
        if all(rp.part_key in player_part_keys for rp in required):
            return gv

    return fallback_no_parts


async def handle_guild_session(
    character: Character,
    player: Player,
    http_client_mod,
    action: str,
    vehicle_name: str,
):
    try:
        if action == "EXITED":
            await _end_active_session(character, http_client_mod)
            return

        guild_vehicle = await _find_matching_guild_vehicle(
            vehicle_name, str(character.guid), http_client_mod
        )

        if guild_vehicle:
            await _activate_guild(character, guild_vehicle, http_client_mod, str(player.unique_id))
        else:
            await _end_active_session(character, http_client_mod)
    except Exception:
        logger.exception(f"Error handling guild session for {character.name}")


async def _end_active_session(character: Character, http_client_mod=None):
    now = timezone.now()
    updated = await GuildSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aupdate(ended_at=now)
    if updated:
        logger.info(f"Ended guild session for {character.name}")
        if http_client_mod:
            await refresh_player_name(character, http_client_mod)


async def _activate_guild(
    character: Character,
    guild_vehicle: GuildVehicle,
    http_client_mod,
    player_id: str,
):
    now = timezone.now()
    guild = guild_vehicle.guild

    try:
        popup_parts = []
        if guild.welcome_message:
            popup_parts.append(guild.welcome_message)

        other_members = [
            s.character.name
            async for s in GuildSession.objects.filter(
                guild=guild, ended_at__isnull=True
            ).exclude(character=character).select_related("character")
        ]
        if other_members:
            popup_parts.append(f"<Bold>Online Members:</> {', '.join(other_members)}")

        if popup_parts:
            await show_popup(
                http_client_mod,
                "\n\n".join(popup_parts),
                character_guid=str(character.guid),
                player_id=player_id,
            )
    except Exception as e:
        logger.error(f"Failed to show guild welcome popup for {character.name}: {e}")

    existing = await GuildSession.objects.filter(
        character=character, guild=guild, ended_at__isnull=True
    ).afirst()
    if existing:
        return

    await _end_active_session(character, http_client_mod)

    await GuildSession.objects.acreate(
        guild=guild,
        character=character,
        started_at=now,
    )
    logger.info(f"Started guild session: {character.name} → {guild.abbreviation}")

    await GuildCharacter.objects.aget_or_create(
        guild=guild,
        character=character,
    )

    await refresh_player_name(character, http_client_mod)

    decal = guild_vehicle.decal
    if decal and decal.config:
        try:
            current = await get_decal(http_client_mod, player_id)
            existing_layers = (current.get("decal") or {}).get("DecalLayers", [])
            if not existing_layers:
                await set_decal(http_client_mod, player_id, decal.config)
        except Exception as e:
            logger.error(f"Failed to apply guild decal for {character.name}: {e}")


async def check_guild_cargo(
    character, cargo_key, payment, damage
) -> tuple[GuildSession | None, int]:
    session = await (
        GuildSession.objects.filter(character=character, ended_at__isnull=True)
        .select_related("guild__cargo_requirement")
        .afirst()
    )
    if not session:
        return None, 0

    try:
        req = session.guild.cargo_requirement
    except Exception:
        return None, 0

    if req.allowed_cargo_keys and cargo_key not in req.allowed_cargo_keys:
        return None, 0
    if req.excluded_cargo_keys and cargo_key in req.excluded_cargo_keys:
        return None, 0
    if req.max_damage is not None and damage > req.max_damage:
        return None, 0
    if req.min_payment is not None and payment < req.min_payment:
        return None, 0
    if req.max_payment is not None and payment > req.max_payment:
        return None, 0

    bonus = int(payment * req.bonus_pct / 100)
    return session, bonus


async def check_guild_passenger(
    character, passenger_type, comfort, urgent, limo, offroad, comfort_rating, payment
) -> tuple[GuildSession | None, int]:
    session = await (
        GuildSession.objects.filter(character=character, ended_at__isnull=True)
        .select_related("guild__passenger_requirement")
        .afirst()
    )
    if not session:
        return None, 0

    try:
        req = session.guild.passenger_requirement
    except Exception:
        return None, 0

    if req.allowed_passenger_types and passenger_type not in req.allowed_passenger_types:
        return None, 0
    if req.require_comfort is not None and comfort != req.require_comfort:
        return None, 0
    if req.require_urgent is not None and urgent != req.require_urgent:
        return None, 0
    if req.require_limo is not None and limo != req.require_limo:
        return None, 0
    if req.require_offroad is not None and offroad != req.require_offroad:
        return None, 0
    if comfort and req.min_comfort_rating is not None and comfort_rating < req.min_comfort_rating:
        return None, 0
    if comfort and req.max_comfort_rating is not None and comfort_rating > req.max_comfort_rating:
        return None, 0

    bonus = int(payment * req.bonus_pct / 100)
    return session, bonus


# ---------------------------------------------------------------------------
# Guild achievements
# ---------------------------------------------------------------------------


def _criteria_matches_passenger(criteria, log):
    if "passenger_type" in criteria and criteria["passenger_type"] != log.passenger_type:
        return False
    if "comfort" in criteria and criteria["comfort"] != log.comfort:
        return False
    if "urgent" in criteria and criteria["urgent"] != log.urgent:
        return False
    if "limo" in criteria and criteria["limo"] != log.limo:
        return False
    if "offroad" in criteria and criteria["offroad"] != log.offroad:
        return False
    if "max_comfort_rating" in criteria:
        if log.comfort_rating is None or log.comfort_rating > criteria["max_comfort_rating"]:
            return False
    if "min_comfort_rating" in criteria:
        if log.comfort_rating is None or log.comfort_rating < criteria["min_comfort_rating"]:
            return False
    if "min_distance" in criteria:
        if log.distance is None or log.distance < criteria["min_distance"]:
            return False
    if "min_payment" in criteria and log.payment < criteria["min_payment"]:
        return False
    return True


def _criteria_matches_cargo(criteria, log):
    if "cargo_key" in criteria and criteria["cargo_key"] != log.cargo_key:
        return False
    if "cargo_key_in" in criteria and log.cargo_key not in criteria["cargo_key_in"]:
        return False
    if "is_illicit" in criteria:
        expected = criteria["is_illicit"]
        actual = log.cargo_key in ILLICIT_CARGO_KEYS
        if expected != actual:
            return False
    if "min_payment" in criteria and log.payment < criteria["min_payment"]:
        return False
    return True


async def evaluate_achievement(guild_character, achievement, log):
    criteria = achievement.criteria
    log_model = criteria.get("log_model", "passenger")

    from amc.models import ServerCargoArrivedLog, ServerPassengerArrivedLog

    if log_model == "passenger":
        if not isinstance(log, ServerPassengerArrivedLog):
            return None, False
        if not _criteria_matches_passenger(criteria, log):
            return None, False
    elif log_model == "cargo":
        if not isinstance(log, ServerCargoArrivedLog):
            return None, False
        if not _criteria_matches_cargo(criteria, log):
            return None, False
    else:
        return None, False

    goal = criteria.get("goal", 1)
    tracking = criteria.get("tracking", "count")

    ca, _created = await GuildCharacterAchievement.objects.aget_or_create(
        guild_character=guild_character,
        achievement=achievement,
    )

    if ca.completed_at is not None:
        return ca.progress, False

    if tracking == "sum_payment":
        ca.progress += log.payment
    else:
        ca.progress += 1

    just_completed = False
    if ca.progress >= goal:
        ca.progress = goal
        ca.completed_at = timezone.now()
        just_completed = True

    await ca.asave()
    return ca.progress, just_completed


async def check_guild_achievements(character, guild_session, log, http_client_mod):
    guild = guild_session.guild
    achievements = [a async for a in guild.achievements.all()]

    if not achievements:
        return

    guild_character = await GuildCharacter.objects.filter(
        guild=guild, character=character
    ).afirst()
    if not guild_character:
        return

    for achievement in achievements:
        try:
            _progress, just_completed = await evaluate_achievement(
                guild_character, achievement, log
            )
            if just_completed:
                await _notify_achievement(character, guild, achievement, guild_session, http_client_mod)
        except Exception:
            logger.exception(
                f"Error evaluating achievement {achievement.name} for {character.name}"
            )


async def _notify_achievement(character, guild, achievement, guild_session, http_client_mod):
    icon = f"{achievement.icon} " if achievement.icon else ""
    popup_text = (
        f"<Achievement>Achievement Unlocked!</>\n"
        f"<Bold>{icon}{achievement.name}</>\n"
        f"{achievement.description}"
    )

    player_id = str(character.player.unique_id)
    character_guid = str(character.guid)

    try:
        await show_popup(
            http_client_mod,
            popup_text,
            player_id=player_id,
            character_guid=character_guid,
        )
    except Exception:
        logger.exception(f"Failed to show achievement popup for {character.name}")

    thread_id = guild.discord_thread_id
    if thread_id:
        from amc.tasks import enqueue_discord_message

        discord_text = f"🏆 **{icon}{achievement.name}** unlocked by **{character.name}**!\n{achievement.description}"
        enqueue_discord_message(thread_id, discord_text, timezone.now())
