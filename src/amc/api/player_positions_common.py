import logging
import time
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.db import connections
from django.db.utils import OperationalError
from django.utils import timezone

from amc.game_server import get_players_locations

logger = logging.getLogger(__name__)

POSITION_UPDATE_RATE = 1
POSITION_UPDATE_SLEEP = 1.0 / POSITION_UPDATE_RATE
HEARTBEAT_INTERVAL = 15
MOD_PLAYERS_CACHE_TTL = 2
POLICE_ONLINE_THRESHOLD_SECONDS = 60
# A C++ snapshot whose capture time is older than this is a FROZEN feed: the
# game-thread sampler publishes at ~2 Hz, so a 5 s gap means it stopped
# publishing (observed when the last player logs out and the sampler skips the
# publish). A frozen snapshot is counted as an EMPTY server — it must never be
# reported as live players.
CPP_SNAPSHOT_MAX_AGE_S = 5.0


def _get_hidden_player_unique_ids_sync():
    from amc.models import Character, PoliceSession, Wanted

    wanted_ids: set[int] = set(
        Wanted.objects.filter(
            wanted_remaining__gt=0, expired_at__isnull=True
        ).values_list("character__player__unique_id", flat=True)
    )

    online_threshold = timezone.now() - timedelta(seconds=POLICE_ONLINE_THRESHOLD_SECONDS)
    police_ids: set[int] = set(
        PoliceSession.objects.filter(
            ended_at__isnull=True, character__last_online__gte=online_threshold
        ).values_list("character__player__unique_id", flat=True)
    )

    costume_ids: set[int] = set(
        Character.objects.filter(
            wearing_costume=True,
            last_online__gte=online_threshold,
        ).values_list("player__unique_id", flat=True)
    )

    return wanted_ids, police_ids, costume_ids


def _get_hidden_player_unique_ids_with_retry():
    try:
        return _get_hidden_player_unique_ids_sync()
    except OperationalError:
        logger.warning("DB connection lost in player positions query, retrying after cleanup")
        connections.close_all()
        return _get_hidden_player_unique_ids_sync()


async def _get_hidden_player_unique_ids():
    return await sync_to_async(_get_hidden_player_unique_ids_with_retry, thread_sensitive=True)()


def _mask_hidden_player(player: dict) -> dict:
    """Copy of `player` with the real position/vehicle removed (privacy)."""
    masked = dict(player)
    masked["Location"] = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    masked["VehicleKey"] = ""
    masked["hidden"] = True
    return masked


def _visible_player_with_flag(player: dict) -> dict:
    flagged = dict(player)
    flagged.setdefault("hidden", False)
    return flagged


async def get_players_mod_masked(
    session,
    cache_key: str = "mod_players_list_all",
    cache_ttl: int = MOD_PLAYERS_CACHE_TTL,
    use_cache: bool = True,
):
    """Full roster for position streaming: hidden players stay in the list but
    carry hidden=True with their location/vehicle zeroed instead of being
    dropped. The mask is the single source of truth — consumers must not
    re-derive or re-apply hiding; they only translate the masked entries."""
    players = await get_players_mod(
        session, cache_key=cache_key, cache_ttl=cache_ttl, use_cache=use_cache
    )
    wanted_ids, police_ids, costume_ids = await _get_hidden_player_unique_ids()
    any_wanted = bool(wanted_ids)
    return [
        _mask_hidden_player(p)
        if _should_hide_player(p, wanted_ids, police_ids, costume_ids, any_wanted)
        else _visible_player_with_flag(p)
        for p in players
    ]


def _should_hide_player(
    player: dict,
    wanted_ids: set[int],
    police_ids: set[int],
    costume_ids: set[int],
    any_wanted: bool,
) -> bool:
    try:
        uid = int(player.get("UniqueID", 0))
    except (ValueError, TypeError):
        return False
    if uid in wanted_ids:
        return True
    if uid in costume_ids:
        return True
    if any_wanted and uid in police_ids:
        return True
    return False


async def get_players_mod(
    session,
    cache_key: str = "mod_players_list_all",
    cache_ttl: int = MOD_PLAYERS_CACHE_TTL,
    filter_hidden: bool = False,
    use_cache: bool = True,
):
    """When `use_cache` is False the roster is always fetched directly — used
    by the position broadcaster, whose 1 s tick would otherwise alternate
    between a fresh snapshot and a duplicated one from the 2 s cache."""
    if use_cache:
        cached_data = cache.get(cache_key)
        if cached_data is not None:
            if not filter_hidden:
                return cached_data
            wanted_ids, police_ids, costume_ids = (
                await _get_hidden_player_unique_ids()
            )
            any_wanted = bool(wanted_ids)
            if not any_wanted and not police_ids and not costume_ids:
                return cached_data
            return [
                p for p in cached_data
                if not _should_hide_player(p, wanted_ids, police_ids, costume_ids, any_wanted)
            ]

    async with session.get("/players") as resp:
        data = await resp.json()
        if not data or not data.get("data"):
            return []
        players = data["data"]
    if use_cache:
        cache.set(cache_key, players, timeout=cache_ttl)

    if filter_hidden:
        wanted_ids, police_ids, costume_ids = await _get_hidden_player_unique_ids()
        any_wanted = bool(wanted_ids)
        if wanted_ids or police_ids or costume_ids:
            return [
                p for p in players
                if not _should_hide_player(p, wanted_ids, police_ids, costume_ids, any_wanted)
            ]

    return players


async def get_positions_masked(session, mgmt_session):
    """Masked roster for the position streams, sourced from the C++ telemetry
    feed when available.

    Location/vehicle come from `GET /players/locations` on the C++ mod
    management API (cheap game-thread reads, no Lua involved); identity
    (UniqueID/PlayerName) comes from the Lua `GET /players` roster read
    through its 2 s cache — at the stream tick the Lua fetch amortizes to
    one HTTP call per cache TTL instead of one per tick. The DB hidden-id
    sets (Wanted / costume / police) are applied here exactly as in
    get_players_mod_masked: the mask is the single source of truth.

    The Lua roster is an IDENTITY source only, never a roster source: when
    the C++ feed is unavailable (`get_players_locations` returns None) or the
    snapshot it serves is frozen (capture time older than
    `CPP_SNAPSHOT_MAX_AGE_S`), the returned roster is EMPTY. A frozen feed's
    last entry is a player who has already left, so replaying it invents a
    phantom player on every stream surface.

    Returns (roster, cpp_ts): `cpp_ts` is the C++ feed's snapshot capture
    time in epoch seconds (from `timestamp_ms` on the mod response), or
    None when the feed is unavailable/frozen — callers fall back to their
    own query time.
    """
    locations = await get_players_locations(mgmt_session, use_cache=False)
    if locations is None:
        return [], None
    identity_players = await get_players_mod(session, use_cache=True)
    roster, cpp_ts = await _merge_masked_roster(locations, identity_players)
    if cpp_ts is None or (time.time() - cpp_ts) > CPP_SNAPSHOT_MAX_AGE_S:
        # Frozen or un-timestamped snapshot: count as empty, not as players.
        return [], None
    return roster, cpp_ts


async def _merge_masked_roster(locations, identity_players):
    wanted_ids, police_ids, costume_ids = await _get_hidden_player_unique_ids()
    any_wanted = bool(wanted_ids)
    by_guid: dict[str, dict] = {}
    for p in identity_players:
        guid = str(p.get("CharacterGuid", "") or "").upper()
        if guid:
            by_guid[guid] = p

    merged = []
    cpp_ts = None
    for e in locations:
        # Snapshot capture time (same on every entry of one response).
        raw_ts = e.get("TimestampMS")
        if raw_ts and cpp_ts is None:
            cpp_ts = float(raw_ts) / 1000.0
        guid = str(e.get("CharacterGuid", "") or "").upper()
        ident = by_guid.get(guid, {})
        player = {
            "UniqueID": str(ident.get("UniqueID", "") or "0"),
            "PlayerName": str(ident.get("PlayerName", "") or ""),
            "CharacterGuid": guid,
            "Location": e.get("Location") or {"X": 0.0, "Y": 0.0, "Z": 0.0},
            "VehicleKey": e.get("VehicleKey") or "",
        }
        if _should_hide_player(player, wanted_ids, police_ids, costume_ids, any_wanted):
            merged.append(_mask_hidden_player(player))
        else:
            merged.append(_visible_player_with_flag(player))
    return merged, cpp_ts
