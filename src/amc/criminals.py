import asyncio
import logging
import math
import time

from datetime import timedelta

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from amc.commands.faction import _build_player_locations, _distance_3d, execute_arrest
from amc.game_server import announce, get_players, get_players_locations
from amc.models import CriminalRecord, PoliceSession, Wanted
from amc.mod_detection import detect_custom_parts, POLICE_DUTY_WHITELIST
from amc.mod_server import clear_suspect, despawn_player_vehicle, force_exit_vehicle, get_player, get_player_customization, get_player_last_vehicle, get_player_last_vehicle_parts, make_suspect, send_system_message, show_popup
from amc.player_tags import refresh_player_name
from amc.special_cargo import announce_money_secured, WANTED_MIN_BOUNTY

SUSPECT_COSTUMES = getattr(settings, "SUSPECT_COSTUMES", frozenset())

logger = logging.getLogger("amc.criminals")

TICK_INTERVAL = 1.0  # seconds between ticks (matches cron cadence)

# Underwater auto-arrest threshold (game units)
UNDERWATER_Z_THRESHOLD = -22455


# Time-based decay reference — online suspects clear in BASE_WANTED_DURATION
# seconds at the base rate (a stationary suspect at the 500 m near cap).
BASE_WANTED_DURATION = Wanted.INITIAL_WANTED_LEVEL  # e.g. 900 s = 15 min
BASE_DECAY_PER_TICK = Wanted.INITIAL_WANTED_LEVEL / BASE_WANTED_DURATION  # = 1.0/tick

# Bounty growth is DISABLED (WANTED_MIN_BOUNTY = 0) — Wanted.amount stays 0.
# The growth spec below is retained as an unexercised code path description.
# Bounty growth — amount ($) added per second while police are nearby (within ESCAPE_DISTANCE).
# Uses 1/r proximity factor (flatter than decay's 1/r²), capped at 1.0 ($100/s).
# At 200m (factor=0.5): growth = $50/s → ~$15k over 5 min chase.
# At REF_DISTANCE (100m, factor=1.0): growth = $100/s (cap).
BOUNTY_GROWTH_PER_TICK = 100  # $/s at reference distance (100m), rate is capped at this

# Logout heat escalation — same 1/r² law as teleport, but capped lower since
# logging out near police is less deliberate than teleporting.
LOGOUT_HEAT_MAX = 300     # max heat added when police are point-blank
LOGOUT_PROXIMITY_RANGE = 200_000  # 2km in game units — no effect beyond this

# --- Speed & distance wanted law + compass cadence (2026-09 rework) ---
# Wanted level is driven by SUSPECT SPEED with a 50 km/h pivot. Distance to
# the nearest on-duty cop only ever HELPS the suspect when FAR away:
#   running (S >= 50 km/h):  dW/dt = +(S - 50) / 50 * A(D)   [s of wanted per s]
#   hiding  (S < 50 km/h):   dW/dt = -(50 - S) / 50 * F(D)
# F(D) accelerates far-from-police hiding (1.0x at the cap -> 3.0x ceiling);
# A(D) slows far-from-police speeding (1.0x at the cap -> 1/3x floor), so
# speeding far away grows wanted slower than speeding next to cops.
# Distance is CLAMPED at the 500 m near cap: inside the ring both multipliers
# are exactly 1.0 — hiding next to a cop decays at the plain speed-driven
# rate. There is NO freeze and NO floor: decay never stalls (the old escape
# gate is gone; freeman correction 2026-09-20).
# Both multipliers share one saturating weight w in [0, 1):
#   w = x/(1+x),  x = max(D - 500 m, 0) / 2000 m
#   F = 1 + 2w    (half the swing at 2.5 km)
#   A = 1 - (2/3)w
# No effective cops on duty (zero on-duty + online + non-AFK): the wanted
# system is DORMANT — organic triggers are blocked and every active ORGANIC
# Wanted record is cleared; admin /setwanted flags survive the dormant
# window (see active_police_present + tick_wanted_countdown).
WANTED_SPEED_PIVOT_KMH = 50.0     # above: wanted grows; below: wanted decays
WANTED_LAW_RATE = 1.0 / 50.0      # s of wanted per (km/h from pivot) per second
HIDE_DECAY_MAX_MULT = 3.0         # F(D) ceiling — far-parked decay multiplier
WANTED_ACCRUAL_MIN_MULT = 1 / 3   # A(D) floor — far-speeding growth multiplier
WANTED_DISTANCE_SCALE_M = 2000.0  # metres past the cap for half the swing
WANTED_NEAR_CAP_UNITS = 50_000    # 500 m in game units — distance clamp


def _distance_weight(dist_units: float) -> float:
    """Saturating weight w in [0, 1) for distance past the 500 m near cap."""
    d_m = max(dist_units - WANTED_NEAR_CAP_UNITS, 0.0) / 100.0
    x = d_m / WANTED_DISTANCE_SCALE_M
    return x / (1.0 + x)


def hide_decay_multiplier(dist_units: float) -> float:
    """F(D): decay multiplier for a hiding suspect, by distance to nearest cop.

    1.0x at the 500 m near cap (and everywhere inside it — the input is
    clamped), rising hyperbolically to HIDE_DECAY_MAX_MULT (3.0x) far away
    (half the bonus WANTED_DISTANCE_SCALE_M past the cap). Never below 1.0:
    decay NEVER slows or stalls because police are close.
    """
    return 1.0 + (HIDE_DECAY_MAX_MULT - 1.0) * _distance_weight(dist_units)


def wanted_accrual_multiplier(dist_units: float) -> float:
    """A(D): growth multiplier for a running suspect, by distance to nearest cop.

    1.0x at the 500 m near cap (and everywhere inside it), falling to
    WANTED_ACCRUAL_MIN_MULT (1/3x) far away — speeding far away grows wanted
    slower than speeding under a cop's nose (freeman correction 2026-09-20).
    """
    return 1.0 - (1.0 - WANTED_ACCRUAL_MIN_MULT) * _distance_weight(dist_units)

# Compass cadence — per-officer interval from THAT officer's distance to the
# suspect and the suspect's speed:
#   interval = 1 / ((D - 500 m) * (S + 20 km/h) * COMPASS_C), clamped
#   [5 s, 120 s]; an officer inside the suspect's 500 m ring gets no updates.
COMPASS_C = 1.5e-6                # Hz per (metre * km/h)
COMPASS_MIN_INTERVAL = 5.0        # seconds — SOLO floor; effective floor 5×N (N receiving cops)
COMPASS_MAX_INTERVAL = 120.0      # seconds — SOLO ceiling; effective ceiling 120×N
COMPASS_HIDE_DISTANCE = 50_000    # 500 m in game units — per-officer silence ring


def compass_interval_seconds(dist_units: float, speed_kmh: float) -> float | None:
    """Per-officer compass update interval; None = silent (inside 500 m ring)."""
    d_m = (dist_units - COMPASS_HIDE_DISTANCE) / 100.0
    if d_m <= 0:
        return None
    prod = d_m * (speed_kmh + 20.0)
    if prod <= 0:
        return COMPASS_MAX_INTERVAL
    return min(
        max(1.0 / (prod * COMPASS_C), COMPASS_MIN_INTERVAL),
        COMPASS_MAX_INTERVAL,
    )

# Tracks the last notified star level per character guid
_last_star_notified: dict[str, int] = {}

# Tracks GUIDs that have had costume state reconciled against the mod server
# (one-shot per backend process to self-heal stale DB state on restart).
_costume_reconciled_guids: set[str] = set()

# Tracks GUIDs that were flagged via the *wanted* pass of
# refresh_suspect_tags on the previous tick.  Used to detect transition-out
# for wanted players (e.g. a wanted record cleared externally or expired
# between ticks) so we can proactively call clear_suspect instead of waiting
# for the mod-side GE duration to expire naturally.
#
# Costume-only criminals are deliberately NOT tracked here: a transient
# `last_online` lag that drops them from the costume queryset for one tick
# must not trigger a transition-out clear.  Clearing on costume removal is
# driven by the ServerSetEquipmentInventory webhook in
# amc/handlers/customization.py, which fires synchronously when the player
# un-equips the costume.
#
# The transition-out pass, however, consults the *combined* suspect set
# (wanted | costume) when deciding whether to clear.  This means a player
# who was wanted last tick but is now only a costume suspect retains the
# GE (no clear_suspect call) — only true transition-to-nothing clears.
_last_suspect_guids: set[str] = set()

# Tracks GUIDs that were in a modded vehicle at the end of the previous
# tick_wanted_countdown pass.  Used to avoid despawning vehicles that a
# wanted player was already sitting in when they became wanted — we only
# despawn on a *transition* from not-in-modded to in-modded (i.e. the
# player entered a modded vehicle while already wanted).
_last_modded_vehicle_guids: set[str] = set()

# Tracks when each (officer guid, suspect guid) pair last received a compass
# update (monotonic clock). Per-officer cadence — each officer's interval is
# keyed on their own distance to the suspect.
_last_compass_sent: dict[tuple[str, str], float] = {}


def _calculate_logout_heat(min_police_distance: float) -> float:
    """Heat added when logging out near police (1/r² law, same as teleport).

    - Point blank (10m):  300 heat (max)
    - 50m:                ~12 heat
    - 100m+:              ~3 heat
    - >2km:               0 (not called)
    """
    clamped_dist = max(min_police_distance, Wanted.MIN_DISTANCE)
    proximity_factor = min(
        Wanted.MAX_DECAY, (Wanted.REF_DISTANCE / clamped_dist) ** 2
    )
    return (proximity_factor / Wanted.MAX_DECAY) * LOGOUT_HEAT_MAX


async def escalate_heat_on_logout(character, http_client, http_client_mod=None) -> None:
    """Auto-arrest when a Wanted player logs out near police.

    If the player is within LOGOUT_PROXIMITY_RANGE of any on-duty police officer,
    treats the logout as an arrest: expires Wanted, confiscates bounty + delivery
    earnings, clears CriminalRecord, and marks the character for jailing on next
    login.  If no police are nearby or the player is too far, falls back to the
    original heat escalation behaviour.
    """
    wanted = await Wanted.objects.filter(
        character=character,
        expired_at__isnull=True,
        wanted_remaining__gt=0,
    ).afirst()
    if not wanted:
        return

    # Need the player's last known location.  If the game has already removed
    # them from the player list we fall back to the cached last_location.
    players = await get_players(http_client)
    locations = _build_player_locations(players) if players else {}

    sus_guid = wanted.character.guid
    if sus_guid not in locations:
        # Player already gone from the server — use last_location if available
        if not character.last_location:
            logger.debug("escalate_heat_on_logout: no location for %s", character.name)
            return
        sus_loc = (character.last_location.x, character.last_location.y, character.last_location.z)
    else:
        _, sus_loc, _ = locations[sus_guid]

    # Find on-duty police
    online_threshold = timezone.now() - timedelta(seconds=60)
    police_sessions = [
        ps
        async for ps in PoliceSession.objects.filter(
            ended_at__isnull=True,
            character__last_online__gte=online_threshold,
        ).select_related("character")
    ]
    cop_locations = [
        locations[ps.character.guid][1]
        for ps in police_sessions
        if ps.character.guid and ps.character.guid in locations
    ]
    if not cop_locations:
        logger.debug("escalate_heat_on_logout: no police online for %s", character.name)
        return

    min_dist = min(_distance_3d(sus_loc, cop_loc) for cop_loc in cop_locations)
    if min_dist > LOGOUT_PROXIMITY_RANGE:
        logger.debug(
            "escalate_heat_on_logout: %s too far from police (%.0f > %.0f)",
            character.name, min_dist, LOGOUT_PROXIMITY_RANGE,
        )
        return

    # Player logged out within range of police — treat as arrest
    if http_client_mod:
        from amc.commands.faction import execute_arrest

        await character.arefresh_from_db(fields=["player"])
        guid = character.guid or str(character.pk)
        targets = {guid: (str(character.player.unique_id), sus_loc, False)}
        target_chars = {guid: character}

        try:
            arrested_names, total_confiscated = await execute_arrest(
                officer_character=None,
                targets=targets,
                target_chars=target_chars,
                http_client=http_client,
                http_client_mod=http_client_mod,
                reason="Arrested for logging out while wanted near police.",
            )
            logger.info(
                "logout arrest: %s — dist=%.0f confiscated=$%d",
                character.name, min_dist, total_confiscated,
            )
            return
        except ValueError as exc:
            logger.warning("logout arrest failed (jail not configured?): %s", exc)
        except Exception:
            logger.exception(
                "logout arrest failed unexpectedly for %s", character.name
            )

    # Fallback: escalate heat if execute_arrest is unavailable or failed
    heat = _calculate_logout_heat(min_dist)
    old_remaining = wanted.wanted_remaining
    old_stars = min(math.ceil(old_remaining / Wanted.LEVEL_PER_STAR), 5)

    max_heat = Wanted.INITIAL_WANTED_LEVEL * 5
    wanted.wanted_remaining = min(max_heat, wanted.wanted_remaining + heat)
    await wanted.asave(update_fields=["wanted_remaining"])

    new_stars = min(math.ceil(wanted.wanted_remaining / Wanted.LEVEL_PER_STAR), 5)
    logger.info(
        "logout heat: %s — dist=%.0f heat=%.1f W%d→W%d",
        character.name, min_dist, heat, old_stars, new_stars,
    )

STAR_MESSAGES = {
    5: "You are wanted. Police are closing in!",
    4: "Your wanted status is decreasing. 4 stars remaining.",
    3: "Your wanted status is decreasing. 3 stars remaining.",
    2: "Your wanted status is decreasing. 2 stars remaining.",
    1: "Your wanted status is almost over. Escape the police to clear it!",
    0: "Your wanted status has expired.",
}


async def _effective_cop_characters(http_client_mod) -> list:
    """Characters of every EFFECTIVE cop: on-duty PoliceSession, online
    (last_online within 60 s) and not AFK (mod ``bAFK`` flag).

    Fail-open per cop: if the mod API cannot confirm a cop's AFK state, the
    cop counts as present — wanted is never amnestied on uncertain data.
    """
    online_threshold = timezone.now() - timedelta(seconds=60)
    sessions = [
        ps
        async for ps in PoliceSession.objects.filter(
            ended_at__isnull=True,
            character__last_online__gte=online_threshold,
        ).select_related("character__player")
    ]
    effective: list = []
    for ps in sessions:
        player_id = str(ps.character.player.unique_id)
        try:
            player_data = await get_player(http_client_mod, player_id)
        except Exception:
            logger.debug(
                "_effective_cop_characters: AFK check failed for %s, assuming present",
                player_id,
            )
            effective.append(ps.character)  # fail open — never amnesty on uncertain data
            continue
        if player_data and player_data.get("bAFK") is True:
            continue  # AFK cop doesn't keep the system armed
        effective.append(ps.character)
    return effective


async def active_police_present(http_client_mod) -> bool:
    """True if at least one EFFECTIVE cop is on duty right now.

    'Effective' = active PoliceSession, online (last_online within 60 s) and
    not AFK (mod ``bAFK`` flag). One parked/AFK cop must not keep the wanted
    system armed overnight (freeman rule 2026-09-20).

    With zero effective cops the wanted system is DORMANT:
      - organic wanted triggers must not fire (gate call sites with this), and
      - every active ORGANIC Wanted record is cleared (dormant amnesty in
        the tick); admin /setwanted flags are preserved while dormant.

    Fail-open: if the mod API cannot confirm a cop's AFK state, the cop
    counts as present — wanted is never amnestied on uncertain data.
    """
    return bool(await _effective_cop_characters(http_client_mod))


async def nearest_effective_cop_distance_m(
    http_client, http_client_mod, character
) -> tuple[bool, float | None]:
    """(cops_present, metres) to the nearest EFFECTIVE cop, for trigger gating.

    Present=False ⇔ zero effective cops on duty ⇒ the wanted system is
    dormant and organic triggers must not fire. Otherwise metres is the
    distance to the nearest effective cop, or None when position data is
    unavailable — callers fail OPEN toward the unattenuated roll (the
    suppression is anti-abuse, not a safety interlock).
    """
    cops = await _effective_cop_characters(http_client_mod)
    if not cops:
        return False, None
    try:
        players = await get_players(http_client)
    except Exception:
        logger.debug(
            "nearest_effective_cop_distance_m: get_players failed, failing open",
            exc_info=True,
        )
        return True, None
    locations = _build_player_locations(players) if players else {}
    target = locations.get(character.guid)
    if target is None:
        return True, None
    from amc.utils import game_units_to_metres

    best_metres: float | None = None
    for cop in cops:
        entry = locations.get(cop.guid)
        if entry is None:
            continue
        metres = game_units_to_metres(_distance_3d(entry[1], target[1]))
        if best_metres is None or metres < best_metres:
            best_metres = metres
    return True, best_metres


async def create_or_refresh_wanted(
    character,
    http_client_mod,
    *,
    amount: int = 0,
    wanted_remaining: int = Wanted.INITIAL_WANTED_LEVEL,
    set_by=None,
) -> tuple[Wanted, bool]:
    """Create or refresh a Wanted record for the given character.

    Returns a tuple of (active Wanted instance, created) where *created*
    is True when a brand-new record was inserted.
    Called by cargo handlers for all illicit cargo types and by police commands.

    Args:
        character: The Character model instance.
        http_client_mod: Mod server HTTP client.
        amount: Additional bounty to accumulate on the Wanted record.
            Typically 0 — bounty grows from police proximity in tick_wanted_countdown.
            Values are floored at WANTED_MIN_BOUNTY.
        wanted_remaining: Initial wanted_remaining value for new or reset records.
            Defaults to 600 seconds (10 minutes).
        set_by: The Character model instance of the police officer who set
            this wanted status (police commands only).
    """

    # Enforce minimum bounty per event.
    effective_amount = max(amount, WANTED_MIN_BOUNTY)
    initial_wanted = wanted_remaining

    created = False
    active_wanted = await Wanted.objects.filter(
        character=character,
        expired_at__isnull=True,
    ).afirst()
    if active_wanted:
        active_wanted.wanted_remaining = initial_wanted
        active_wanted.amount = F("amount") + effective_amount
        await active_wanted.asave(update_fields=["wanted_remaining", "amount"])
        await active_wanted.arefresh_from_db(fields=["amount"])
    else:
        active_wanted = await Wanted.objects.acreate(
            character=character,
            wanted_remaining=initial_wanted,
            amount=effective_amount,
            set_by=set_by,
        )
        created = True

    await refresh_player_name(character, http_client_mod)
    asyncio.create_task(
        send_system_message(
            http_client_mod,
            "You are wanted. Police are closing in!",
            character_guid=character.guid,
        )
    )

    # Set the player as a suspect in-game so police can chase them
    if http_client_mod and character.guid:
        try:
            await make_suspect(http_client_mod, character.guid)
        except Exception:
            logger.warning(
                "make_suspect failed for %s (guid=%s)",
                character.name, character.guid, exc_info=True,
            )

    # Seed modded-vehicle tracking so the first tick after becoming
    # wanted does not immediately despawn a vehicle the player was
    # already sitting in.  Only transition from not-in-modded →
    # in-modded triggers despawn.
    if http_client_mod and character.guid:
        try:
            last_vehicle, parts_data = await asyncio.gather(
                get_player_last_vehicle(http_client_mod, character.guid),
                get_player_last_vehicle_parts(http_client_mod, character.guid, complete=False),
            )
            main_vehicle = last_vehicle.get("vehicle")
            if main_vehicle:
                whitelist = None
                is_on_duty = await PoliceSession.objects.filter(
                    character=character, ended_at__isnull=True
                ).aexists()
                if is_on_duty:
                    whitelist = POLICE_DUTY_WHITELIST
                custom_parts = detect_custom_parts(
                    parts_data.get("parts", []), whitelist=whitelist
                )
                if custom_parts:
                    _last_modded_vehicle_guids.add(character.guid)
        except Exception:
            pass  # Best effort — if we can't determine vehicle state, skip seeding

    return active_wanted, created


def compute_stars(wanted_remaining: float) -> int:
    """Compute the star level (1–5) from remaining wanted heat."""
    if wanted_remaining <= 0:
        return 0
    return min(math.ceil(wanted_remaining / Wanted.LEVEL_PER_STAR), 5)


# Internal alias kept for use within this module
_compute_stars = compute_stars


async def tick_wanted_countdown(http_client, http_client_mod, http_client_mgmt=None) -> None:
    """Single tick of the wanted countdown. Called from an arq cron.

    Speed-based wanted law (2026-09 rework, corrected 2026-09-20):
        running (S >= 50 km/h):  wanted GROWS at (S - 50)/50 * A(D) s per
                                 second, capped at INITIAL_WANTED_LEVEL (5★);
                                 A(D) falls from 1.0x at the 500 m near cap
                                 to 1/3x far away (speeding far builds slower)
        hiding  (S < 50 km/h):   wanted DECAYS at (50 - S)/50 * F(D) s per
                                 second; F(D) rises from 1.0x at the 500 m
                                 near cap to 3.0x far away

    Distance NEVER slows or freezes decay — the old escape gate is gone.
    Inside the 500 m cap both multipliers are exactly 1.0: hiding next to a
    cop clears at the plain speed-driven rate.

    Dormant rule (freeman 2026-09-20): with zero effective cops on duty
    (on-duty + online + non-AFK — see active_police_present) the wanted
    system is off. No decay, no growth, no underwater arrests, no modded
    despawns, and every active ORGANIC Wanted record is cleared: online
    suspects get the normal expiry flow, offline suspects are expired
    silently. Admin /setwanted flags (set_by set) survive the dormant
    window at their current heat; the normal speed law resumes once a cop
    goes back on duty.

    Offline suspects (while armed): no decay, wanted persists indefinitely.
    """
    # Batch-load all active wanted records
    wanted_list = [
        w
        async for w in Wanted.objects.filter(
            expired_at__isnull=True,
            wanted_remaining__gt=0,
        ).select_related("character__player")
    ]
    if not wanted_list:
        return
    logger.info("wanted tick: %d active records", len(wanted_list))

    # --- Dormant amnesty: no effective cops on duty -> clear ORGANIC heat ---
    # Admin /setwanted flags (set_by set) are deliberate and cop-independent:
    # the command itself is exempt from the dormant gate, so its output must
    # survive it too. They are preserved at their current heat while dormant
    # and the normal speed law resumes once a cop goes back on duty
    # (freeman 2026-09-20: setwanted defaults to full wanted_remaining).
    if not await active_police_present(http_client_mod):
        organic = [w for w in wanted_list if w.set_by_id is None]
        admin_flags = [w for w in wanted_list if w.set_by_id is not None]
        if organic:
            await Wanted.objects.filter(
                id__in=[w.id for w in organic]
            ).aupdate(wanted_remaining=0, expired_at=timezone.now())
            # Online suspects get the normal expiry flow; offline suspects
            # are expired silently (their suspect GE is only maintained
            # while an active Wanted row exists, so nothing to undo
            # game-side).
            players = await get_players(http_client)
            locations = _build_player_locations(players) if players else {}
            online_chars = [
                w.character for w in organic if w.character.guid in locations
            ]
            logger.info(
                "wanted tick: no effective cops on duty — dormant amnesty "
                "cleared %d organic records (%d online); "
                "%d admin flag(s) preserved",
                len(organic),
                len(online_chars),
                len(admin_flags),
            )
            await _finalize_expired_wanted(
                online_chars, http_client, http_client_mod
            )
        elif admin_flags:
            logger.info(
                "wanted tick: dormant — %d admin flag(s) preserved",
                len(admin_flags),
            )
        return

    # Fetch player locations (best-effort; empty is fine)
    players = await get_players(http_client)
    locations = _build_player_locations(players) if players else {}

    # Speed telemetry from the mod management API (game units/s).
    # Missing entry or unavailable API -> treat as stationary (full decay).
    speed_map: dict[str, float] = {}
    if http_client_mgmt:
        try:
            mgmt_locations = await get_players_locations(http_client_mgmt)
        except Exception:  # noqa: BLE001 — graceful degradation to stationary
            mgmt_locations = None
        if mgmt_locations:
            speed_map = {e["CharacterGuid"]: e["Speed"] for e in mgmt_locations}

    # Identify on-duty police officers (only if we have locations)
    cop_locations = []
    if locations:
        online_threshold = timezone.now() - timedelta(seconds=60)
        police_sessions = [
            ps
            async for ps in PoliceSession.objects.filter(
                ended_at__isnull=True,
                character__last_online__gte=online_threshold,
        ).select_related("character__player")
        ]
        cop_guids = {
            ps.character.guid
            for ps in police_sessions
            if ps.character.guid and ps.character.guid in locations
        }
        for cg in cop_guids:
            _, cop_loc, _ = locations[cg]
            cop_locations.append(cop_loc)

    expired_characters = []
    star_change_notifications = []  # (wanted, message) for deferred processing
    _current_modded_guids: set[str] = set()  # modded vehicle state this tick

    for wanted in wanted_list:
        sus_guid = wanted.character.guid
        old_stars = _compute_stars(wanted.wanted_remaining)

        # Offline suspect → no decay, wanted persists
        if sus_guid not in locations:
            continue

        _, sus_loc, _ = locations[sus_guid]

        # Underwater suspects are automatically arrested
        if sus_loc[2] < UNDERWATER_Z_THRESHOLD:
            if http_client_mod:
                targets = {
                    sus_guid: (
                        str(wanted.character.player.unique_id),
                        sus_loc,
                        False,
                    )
                }
                target_chars = {sus_guid: wanted.character}
                try:
                    arrested_names, total_confiscated = await execute_arrest(
                        officer_character=None,
                        targets=targets,
                        target_chars=target_chars,
                        http_client=http_client,
                        http_client_mod=http_client_mod,
                        reason="Arrested for going underwater while wanted.",
                    )
                    logger.info(
                        "underwater arrest: %s — z=%.0f confiscated=$%d",
                        wanted.character.name,
                        sus_loc[2],
                        total_confiscated,
                    )
                except ValueError as exc:
                    logger.warning(
                        "underwater arrest failed (jail not configured?): %s", exc
                    )
                except Exception:
                    logger.exception(
                        "underwater arrest failed unexpectedly for %s",
                        wanted.character.name,
                    )
            _last_star_notified.pop(sus_guid, None)
            continue

        # Modded-vehicle despawn for wanted players
        # Only despawn when a wanted player *enters* a modded vehicle
        # (transition from not-in-modded → in-modded).  Players who
        # were already in a modded vehicle when they became wanted are
        # seeded into _last_modded_vehicle_guids by create_or_refresh_wanted.
        currently_in_modded = False
        if http_client_mod:
            try:
                last_vehicle, parts_data = await asyncio.gather(
                    get_player_last_vehicle(http_client_mod, sus_guid),
                    get_player_last_vehicle_parts(http_client_mod, sus_guid, complete=False),
                )
                main_vehicle = last_vehicle.get("vehicle")
                if main_vehicle:
                    whitelist = None
                    is_on_duty = await PoliceSession.objects.filter(
                        character=wanted.character, ended_at__isnull=True
                    ).aexists()
                    if is_on_duty:
                        whitelist = POLICE_DUTY_WHITELIST
                    custom_parts = detect_custom_parts(
                        parts_data.get("parts", []), whitelist=whitelist
                    )
                    if custom_parts:
                        currently_in_modded = True
                        if sus_guid not in _last_modded_vehicle_guids:
                            try:
                                await force_exit_vehicle(http_client_mod, sus_guid)
                                await despawn_player_vehicle(http_client_mod, sus_guid)
                                await show_popup(
                                    http_client_mod,
                                    "Your modded vehicle has been despawned because you are wanted by police.",
                                    character_guid=sus_guid,
                                    player_id=str(wanted.character.player.unique_id),
                                )
                                logger.info(
                                    "modded vehicle despawn: %s",
                                    wanted.character.name,
                                )
                            except Exception:
                                logger.exception(
                                    "modded vehicle despawn failed for %s",
                                    wanted.character.name,
                                )
            except Exception:
                logger.debug(
                    "tick_wanted_countdown: mod check failed for %s, skipping",
                    wanted.character.name,
                )

        if currently_in_modded:
            _current_modded_guids.add(sus_guid)

        # --- Speed-based wanted law (corrected 2026-09-20) ---
        # Running (>= 50 km/h): grow, scaled by A(D) — speeding far builds
        # wanted SLOWER (1/3x floor), never faster (near cap = 1.0x).
        # Hiding (< 50 km/h): decay, scaled by F(D) — hiding far clears
        # FASTER (3x ceiling), and near cops decay runs at the base rate.
        # No gate, no floor: the meter always moves with the suspect's speed.
        speed_units = speed_map.get(sus_guid.upper(), 0.0)
        speed_kmh = speed_units * 0.036  # game units/s -> km/h

        min_dist = None
        if cop_locations:
            min_dist = min(_distance_3d(sus_loc, cop_loc) for cop_loc in cop_locations)

        if speed_kmh >= WANTED_SPEED_PIVOT_KMH:
            growth = (
                (speed_kmh - WANTED_SPEED_PIVOT_KMH)
                * WANTED_LAW_RATE
                * TICK_INTERVAL
            )
            if min_dist is not None:
                growth *= wanted_accrual_multiplier(min_dist)
            wanted.wanted_remaining = min(
                float(Wanted.INITIAL_WANTED_LEVEL),
                wanted.wanted_remaining + growth,
            )
        else:
            # Distance never slows decay: F(D) >= 1.0 everywhere (clamped
            # at the 500 m near cap), so point-blank hiding decays at the
            # plain speed-driven rate and hiding far clears faster.
            if min_dist is not None:
                mult = hide_decay_multiplier(min_dist)
            else:
                mult = 1.0
            decay = (
                (WANTED_SPEED_PIVOT_KMH - speed_kmh)
                * WANTED_LAW_RATE
                * mult
                * TICK_INTERVAL
            )
            wanted.wanted_remaining = max(
                0.0, wanted.wanted_remaining - decay
            )
            if wanted.wanted_remaining <= 0:
                expired_characters.append(wanted.character)

        # Track star changes for deferred notification
        new_stars = _compute_stars(wanted.wanted_remaining)
        if new_stars != old_stars:
            last_notified = _last_star_notified.get(sus_guid)
            if last_notified is None or new_stars != last_notified:
                _last_star_notified[sus_guid] = new_stars
                msg = STAR_MESSAGES.get(new_stars)
                star_change_notifications.append((wanted, msg))

    # Update modded-vehicle tracking for next tick
    _last_modded_vehicle_guids.clear()
    _last_modded_vehicle_guids.update(_current_modded_guids)

    # Bulk save — must happen BEFORE refresh_player_name so it reads correct DB state
    await Wanted.objects.abulk_update(wanted_list, ["wanted_remaining", "amount"])

    # Mark expired (set expired_at instead of deleting)
    expired_ids = [w.id for w in wanted_list if w.wanted_remaining <= 0]
    if expired_ids:
        logger.info(
            "wanted tick: %d records expired — %s",
            len(expired_ids),
            [c.name for c in expired_characters],
        )
        await Wanted.objects.filter(id__in=expired_ids).aupdate(
            wanted_remaining=0,
            expired_at=timezone.now(),
        )

    # Send star-change messages and refresh names (DB is now up-to-date)
    refreshed_guids = set()
    for wanted, msg in star_change_notifications:
        sus_guid = wanted.character.guid
        if msg:
            try:
                await send_system_message(
                    http_client_mod,
                    msg,
                    character_guid=sus_guid,
                )
            except Exception:
                logger.warning(
                    f"Failed to send wanted star message to {wanted.character.name}"
                )
        try:
            await refresh_player_name(wanted.character, http_client_mod)
            refreshed_guids.add(sus_guid)
        except Exception:
            logger.warning(
                f"Failed to refresh name for {wanted.character.name} after star change"
            )

    # Refresh names + announcements + suspect-GE cleanup for expired suspects
    await _finalize_expired_wanted(
        expired_characters, http_client, http_client_mod,
        skip_name_refresh=refreshed_guids,
    )


async def _finalize_expired_wanted(
    characters,
    http_client,
    http_client_mod,
    *,
    skip_name_refresh: set[str] | None = None,
) -> None:
    """Shared expiry flow for characters whose Wanted record just ended.

    Used by the normal tick expiry AND the dormant amnesty (no cops on duty).
    Offline characters are skipped entirely (nothing to undo game-side).
    """
    if not characters:
        return
    if skip_name_refresh is None:
        skip_name_refresh = set()
    for char in characters:
        _last_star_notified.pop(char.guid, None)
        if char.guid not in skip_name_refresh:
            try:
                await refresh_player_name(char, http_client_mod)
            except Exception:
                logger.warning(
                    f"Failed to refresh name for {char.name} after wanted expired"
                )
        if char.guid:
            try:
                await announce(
                    f"{char.name} is no longer wanted by police",
                    http_client,
                    color="43B581",
                )
            except Exception:
                logger.warning(f"Failed to announce freedom for {char.name}")
            try:
                await announce_money_secured(char.guid, http_client)
            except Exception:
                logger.warning(f"Failed to announce money secured for {char.name}")
            # Immediately drop the in-game suspect GE so the blue overlay and
            # Net_Suspects entry disappear within the same tick rather than
            # waiting up to ~60 s for the mod-side GE duration to expire.
            # refresh_suspect_tags would also clear this on its next 30 s
            # pass, but we want instant feedback when a chase ends.
            #
            # However, if the player is still wearing a suspect costume
            # (active CriminalRecord + wearing_costume=True), the costume
            # pass of refresh_suspect_tags will re-flag them within 30 s —
            # clearing here would cause a visible gap in the blue overlay.
            # Reapply make_suspect instead to reset the 60 s cap cleanly
            # and keep them as a suspect.
            if http_client_mod:
                still_costume_suspect = await CriminalRecord.objects.filter(
                    character=char,
                    cleared_at__isnull=True,
                    character__wearing_costume=True,
                ).aexists()
                if still_costume_suspect:
                    try:
                        await make_suspect(
                            http_client_mod,
                            char.guid,
                            duration_seconds=CRIMINAL_SUSPECT_DURATION,
                        )
                    except Exception:
                        logger.warning(
                            "make_suspect (post-wanted costume) failed for %s",
                            char.name,
                        )
                    # Still a suspect → stay in the tracked set so the next
                    # refresh_suspect_tags transition-out pass doesn't clear us.
                    _last_suspect_guids.add(char.guid)
                else:
                    try:
                        await clear_suspect(http_client_mod, char.guid)
                    except Exception:
                        logger.warning(
                            "clear_suspect failed for %s after wanted expired",
                            char.name,
                        )
                    _last_suspect_guids.discard(char.guid)


# ---------------------------------------------------------------------------
# Criminal Record decay
# ---------------------------------------------------------------------------

CRIMINAL_RECORD_HALF_LIFE_MINUTES = 120  # 2 hours of online time
CRIMINAL_RECORD_DECAY_FACTOR = 0.5 ** (1 / CRIMINAL_RECORD_HALF_LIFE_MINUTES)
CRIMINAL_RECORD_DECAY_FLOOR = 5000  # confiscatable amounts below this are zeroed out and record is closed
ONLINE_THRESHOLD_SECONDS = 60  # character considered online if last_online < 60s ago
CRIMINAL_SUSPECT_DURATION = 70  # seconds — mod clamps to 60s; refresh_suspect_tags reapplies every 30s for overlap


async def refresh_suspect_tags(http_client_mod) -> None:
    """Re-apply the suspect flag to every online wanted player and to every
    online active criminal wearing a costume.

    Called every 30 seconds via arq cron (see ``WorkerSettings.cron_jobs``).
    The mod server currently caps the suspect GE duration at 60 s regardless
    of the ``DurationSeconds`` we pass, so this cadence must be strictly
    less than 60 s to prevent the status from lapsing between ticks.

    Gating is driven entirely off DB state (``character.last_online`` +
    ``wearing_costume`` + active ``Wanted``/``CriminalRecord``) — not the
    mod server's transient ``/players`` snapshot.  This avoids dropping
    legitimate suspects when the mod's player list momentarily misses a
    GUID (2 s cache miss, brief API hiccup, missing ``location`` field
    while loading in, etc.) which previously caused the GE to expire.

    Emits ``clear_suspect`` only for players whose *combined* suspect
    status (wanted OR wearing-costume) transitioned to cleared — so a
    wanted→not-wanted transition while the player is still wearing a
    costume preserves the suspect GE.
    """
    online_cutoff = timezone.now() - timedelta(seconds=ONLINE_THRESHOLD_SECONDS)

    # --- Wanted pass ---
    # DB is the source of truth.  Every online wanted player is re-flagged
    # every tick regardless of whether the mod's player list currently
    # reports them.
    wanted_guids: set[str] = set()
    wanted_list = [
        w
        async for w in Wanted.objects.filter(
            expired_at__isnull=True,
            wanted_remaining__gt=0,
            character__guid__isnull=False,
            character__last_online__gte=online_cutoff,
        ).select_related("character")
    ]

    for wanted in wanted_list:
        sus_guid = wanted.character.guid
        if not sus_guid:
            continue
        # Pass at least CRIMINAL_SUSPECT_DURATION so the duration never
        # collapses to 1 s for a nearly-cleared suspect.  The
        # mod currently clamps to 60 s anyway, but this future-proofs the
        # call for when it honours the passed value.
        duration_seconds = math.ceil(
            wanted.wanted_remaining / BASE_DECAY_PER_TICK * TICK_INTERVAL
        )
        try:
            await make_suspect(
                http_client_mod,
                sus_guid,
                duration_seconds=max(CRIMINAL_SUSPECT_DURATION, duration_seconds),
            )
            wanted_guids.add(sus_guid)
        except Exception:
            logger.warning("Failed to make suspect for %s", wanted.character.name)

    # --- Costume criminal pass ---
    # DB-gated: active CriminalRecord + wearing_costume=True + online.
    # Note: costume GUIDs are NOT added to _last_suspect_guids (see the
    # module-level comment on that set).  They ARE collected into
    # costume_guids for use by the transition-out pass below, which
    # consults the combined wanted|costume set to decide whether to clear.
    costume_guids: set[str] = set()
    costume_criminals = CriminalRecord.objects.filter(
        cleared_at__isnull=True,
        character__wearing_costume=True,
        character__guid__isnull=False,
        character__last_online__gte=online_cutoff,
    ).select_related("character")

    async for rec in costume_criminals:
        guid = rec.character.guid
        costume_guids.add(guid)
        if guid in wanted_guids:
            # Already refreshed via the wanted pass with the wanted-derived
            # duration; skip the costume re-apply but keep the guid in
            # costume_guids so the transition-out set sees it as "still
            # costume-suspect" if the wanted record clears before next tick.
            continue
        try:
            await make_suspect(
                http_client_mod, guid, duration_seconds=CRIMINAL_SUSPECT_DURATION,
            )
        except Exception:
            logger.warning("costume make_suspect failed for %s", rec.character.name)

    # --- Reconciliation: one-shot costume hydration for online criminals ---
    unreconciled_criminals = CriminalRecord.objects.filter(
        cleared_at__isnull=True,
        character__guid__isnull=False,
        character__last_online__gte=online_cutoff,
    ).exclude(
        character__guid__in=_costume_reconciled_guids,
    ).select_related("character")

    async for rec in unreconciled_criminals:
        guid = rec.character.guid
        _costume_reconciled_guids.add(guid)
        try:
            customization = await get_player_customization(http_client_mod, guid)
            if customization is None:
                continue
            costume_key = customization.get("Costume") or None
            wearing = costume_key in SUSPECT_COSTUMES
            if wearing != rec.character.wearing_costume or costume_key != rec.character.costume_item_key:
                rec.character.wearing_costume = wearing
                rec.character.costume_item_key = costume_key
                await rec.character.asave(update_fields=["wearing_costume", "costume_item_key"])
                if wearing and guid not in wanted_guids and guid not in costume_guids:
                    try:
                        await make_suspect(
                            http_client_mod, guid, duration_seconds=CRIMINAL_SUSPECT_DURATION,
                        )
                        costume_guids.add(guid)
                    except Exception:
                        logger.warning("reconciliation make_suspect failed for %s", rec.character.name)
        except Exception:
            logger.debug("reconciliation poll failed for %s", rec.character.name)

    # --- Transition-out pass ---
    # A GUID only transitions out when it is NEITHER wanted nor wearing a
    # costume this tick.  This prevents clearing the suspect GE from a
    # wanted player who also happens to be wearing a costume (or vice
    # versa) when one of the two conditions clears but the other is still
    # active.
    #
    # The tracking set itself (_last_suspect_guids) is wanted-only — see the
    # module-level comment.  This keeps costume criminals immune to the
    # last_online-lag flicker bug while still preventing false clears on
    # wanted-to-costume transitions via the combined diff here.
    currently_suspect = wanted_guids | costume_guids
    transitioned_out = _last_suspect_guids - currently_suspect
    for guid in transitioned_out:
        try:
            await clear_suspect(http_client_mod, guid)
        except Exception:
            logger.warning("clear_suspect failed for transitioned-out guid %s", guid)

    _last_suspect_guids.clear()
    _last_suspect_guids.update(wanted_guids)


async def tick_police_suspect_locations(http_client, http_client_mod, http_client_mgmt) -> None:
    """Send every on-duty police officer a combined system message showing
    distance and bearing for each online wanted suspect.

    Update cadence is per-officer, keyed on THAT officer's distance to the
    suspect and the suspect's speed:

        solo = 1 / ((D - 500 m) * (S + 20 km/h) * COMPASS_C)

    clamped to [5 s, 120 s], then scaled by the FORCE BUDGET: the interval is
    multiplied by N, the number of on-duty officers beyond their own 500 m
    ring for that suspect. The force's total flash rate for one suspect stays
    at ONE cop's rate no matter how many are watching — extra cops split the
    budget instead of multiplying it (freeman, 2026-09-20). Effective range
    [5N, 120N] s; cross-officer flashes land ~N× further apart in time, so a
    moving suspect de-correlates between officers instead of being
    triangulated. An officer inside the suspect's 500 m silence ring gets no
    updates for that suspect at all. Missing speed telemetry degrades to the
    stationary cadence.
    """
    wanted_list = [
        w
        async for w in Wanted.objects.filter(
            expired_at__isnull=True,
            wanted_remaining__gt=0,
        ).select_related("character")
    ]
    if not wanted_list:
        _last_compass_sent.clear()
        return

    players = await get_players(http_client)
    locations = _build_player_locations(players) if players else {}
    if not locations:
        return

    # Speed telemetry from mod management API (game units/s).
    # Falls back to the stationary cadence if the API is unavailable.
    try:
        mgmt_locations = await get_players_locations(http_client_mgmt)
    except Exception:  # noqa: BLE001 — graceful degradation to stationary cadence
        mgmt_locations = None
    speed_map: dict[str, float] = {}
    if mgmt_locations:
        speed_map = {e["CharacterGuid"]: e["Speed"] for e in mgmt_locations}

    # Pre-compute online suspect (character, location) pairs
    wanted_guids: set[str] = set()
    online_suspects = []
    for wanted in wanted_list:
        guid = wanted.character.guid
        if not guid:
            continue
        wanted_guids.add(guid)
        if guid not in locations:
            continue
        online_suspects.append((wanted.character, locations[guid][1]))

    if not online_suspects:
        # Purge stale compass timestamps
        for stale in set(_last_compass_sent):
            if stale[1] not in wanted_guids:
                del _last_compass_sent[stale]
        return

    from amc.police import get_active_police_characters
    from amc.utils import compass_heading, game_units_to_metres

    police_chars = await get_active_police_characters()

    # Collect all officer locations first
    officer_entries = []  # (officer, officer_loc)
    async for officer in police_chars:
        officer_guid = officer.guid
        if not officer_guid or officer_guid not in locations:
            continue
        _, officer_loc, _ = locations[officer_guid]
        officer_entries.append((officer, officer_loc))

    if not officer_entries:
        return

    # Force-level budget (freeman, 2026-09-20): count the officers who would
    # RECEIVE flashes for each suspect (on-duty cops beyond their own 500 m
    # ring). Every receiving officer's interval is multiplied by N below, so
    # N cops share one cop's cadence instead of each running their own.
    receiving_counts: dict[str, int] = {}
    for character, suspect_loc in online_suspects:
        count = 0
        for officer, officer_loc in officer_entries:
            if officer.guid == character.guid:
                continue
            if _distance_3d(officer_loc, suspect_loc) > COMPASS_HIDE_DISTANCE:
                count += 1
        receiving_counts[character.guid] = count

    now = time.monotonic()
    for officer, officer_loc in officer_entries:
        officer_guid = officer.guid
        officer_x, officer_y, officer_z = officer_loc

        entries = []  # (distance, formatted_line)
        for character, suspect_loc in online_suspects:
            # Wanted players should never be police, but guard anyway
            if character.guid == officer_guid:
                continue

            dist = _distance_3d(officer_loc, suspect_loc)
            speed_kmh = speed_map.get(character.guid.upper(), 0.0) * 0.036
            interval = compass_interval_seconds(dist, speed_kmh)
            if interval is None:
                # This officer is inside the suspect's 500 m silence ring
                continue
            # Force budget: split one cop's cadence across every receiving
            # officer instead of letting each run their own stream.
            interval *= receiving_counts.get(character.guid, 1)
            key = (officer_guid, character.guid)
            if now - _last_compass_sent.get(key, 0.0) < interval:
                continue
            _last_compass_sent[key] = now

            metres = game_units_to_metres(dist)

            dx = suspect_loc[0] - officer_x
            dy = suspect_loc[1] - officer_y
            direction = compass_heading(dx, dy)

            if metres < 1000:
                dist_str = f"{metres}m"
            else:
                dist_str = f"{metres / 1000:.1f}km"

            entries.append((dist, f"[{character.name}] {dist_str} {direction}"))

        if not entries:
            continue

        entries.sort(key=lambda x: x[0])
        message = "\n".join(line for _, line in entries)

        try:
            await send_system_message(
                http_client_mod,
                message,
                character_guid=officer_guid,
            )
        except Exception:
            logger.warning(
                "Failed to send suspect locations to officer %s", officer.name
            )

    # Purge stale (officer, suspect) compass timestamps
    officer_guids = {officer.guid for officer, _ in officer_entries}
    valid_keys = {
        (og, sg) for og in officer_guids for sg in wanted_guids
    }
    for stale in set(_last_compass_sent) - valid_keys:
        del _last_compass_sent[stale]


async def tick_criminal_record_decay(http_client_mod=None) -> None:
    """Decay confiscatable_amount for ONLINE characters only.

    Called every minute via arq cron. Applies exponential decay with a
    2-hour half-life of *online time*. Offline criminals preserve their
    confiscatable amount so they cannot escape punishment by logging off.

    Players currently in a modded vehicle are excluded from decay —
    their confiscatable amount is preserved as long as they remain in
    a modified vehicle.

    Players who are AFK (bAFK=true) are also excluded from decay.

    The `amount` field is NEVER decayed — it is a permanent audit trail.
    """
    online_cutoff = timezone.now() - timedelta(seconds=ONLINE_THRESHOLD_SECONDS)

    records = [
        r
        async for r in CriminalRecord.objects.filter(
            cleared_at__isnull=True,
            character__last_online__gte=online_cutoff,
        ).select_related("character__player")
    ]
    if not records:
        return

    modded_guids: set[str] = set()
    afk_player_ids: set[str] = set()
    if http_client_mod:
        from amc.mod_server import get_player, get_player_last_vehicle, get_player_last_vehicle_parts
        from amc.mod_detection import detect_custom_parts

        for record in records:
            player_id = str(record.character.player.unique_id)

            # AFK check
            try:
                player_data = await get_player(http_client_mod, player_id)
                if player_data and player_data.get("bAFK"):
                    afk_player_ids.add(player_id)
                    continue
            except Exception:
                logger.debug(
                    "tick_criminal_record_decay: afk check failed for %s, skipping",
                    record.character.name,
                )

            # Modded vehicle check
            guid = record.character.guid
            if not guid:
                continue
            try:
                last_vehicle, parts_data = await asyncio.gather(
                    get_player_last_vehicle(http_client_mod, guid),
                    get_player_last_vehicle_parts(http_client_mod, guid, complete=False),
                )
                main_vehicle = last_vehicle.get("vehicle")
                if not main_vehicle:
                    modded_guids.add(guid)
                    continue
                whitelist = None
                is_on_duty = await PoliceSession.objects.filter(
                    character=record.character, ended_at__isnull=True
                ).aexists()
                if is_on_duty:
                    whitelist = POLICE_DUTY_WHITELIST
                custom_parts = detect_custom_parts(
                    parts_data.get("parts", []), whitelist=whitelist
                )
                if custom_parts:
                    modded_guids.add(guid)
            except Exception:
                logger.debug(
                    "tick_criminal_record_decay: mod check failed for %s, skipping",
                    record.character.name,
                )

    decayed = []
    closed_records = []
    for record in records:
        preserve_amount = (
            record.character.guid in modded_guids
            or str(record.character.player.unique_id) in afk_player_ids
        )
        if not preserve_amount:
            record.confiscatable_amount = int(
                record.confiscatable_amount * CRIMINAL_RECORD_DECAY_FACTOR
            )
        # Close when below floor, unless the character is wearing a criminal
        # costume (the C tag must persist for the full duration of wear).
        # Closure runs even for modded/AFK players — the preservation above
        # keeps their amount intact, but once it's below floor there is
        # nothing left to preserve and the record has served its purpose.
        if record.confiscatable_amount < CRIMINAL_RECORD_DECAY_FLOOR and not record.character.wearing_costume:
            record.confiscatable_amount = 0
            record.cleared_at = timezone.now()
            closed_records.append(record)
        decayed.append(record)

    if not decayed:
        return
    await CriminalRecord.objects.abulk_update(decayed, ["confiscatable_amount", "cleared_at"])
    if closed_records:
        logger.info(
            "tick_criminal_record_decay: closed %d record(s) that decayed below floor",
            len(closed_records),
        )
        if http_client_mod:
            for record in closed_records:
                try:
                    await refresh_player_name(record.character, http_client_mod)
                except Exception:
                    logger.warning(
                        "Failed to refresh name for %s after criminal record closed",
                        record.character.name,
                    )
    logger.debug(
        "tick_criminal_record_decay: decayed %d record(s) (skipped %d modded, %d afk)",
        len(decayed),
        len(modded_guids),
        len(afk_player_ids),
    )
