import asyncio
import logging
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.db import connections
from django.db.utils import OperationalError
from django.utils import timezone

logger = logging.getLogger(__name__)

POSITION_UPDATE_RATE = 1
POSITION_UPDATE_SLEEP = 1.0 / POSITION_UPDATE_RATE
HEARTBEAT_INTERVAL = 15
MOD_PLAYERS_CACHE_TTL = 2
POLICE_ONLINE_THRESHOLD_SECONDS = 60


def _get_hidden_player_unique_ids_sync():
    from amc.models import CriminalRecord, PoliceSession, Wanted

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
        CriminalRecord.objects.filter(
            cleared_at__isnull=True,
            character__wearing_costume=True,
            character__last_online__gte=online_threshold,
        ).values_list("character__player__unique_id", flat=True)
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
):
    cached_data = cache.get(cache_key)
    if cached_data is not None:
        if not filter_hidden:
            return cached_data
        wanted_ids, police_ids, costume_ids = await _get_hidden_player_unique_ids()
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


class PlayerPositionsSubscription:
    """Shared scheduled task that emits player positions to all subscribers.

    One background loop fetches + processes mod player data every
    `POSITION_UPDATE_SLEEP` seconds.  Consumers subscribe with an
    ``async for`` loop and unsubscribe automatically on `break`.
    When the subscriber count drops to zero the loop stops to save
    resources.
    """

    _task: asyncio.Task | None = None
    _subs: set[asyncio.Queue] = set()
    _lock = asyncio.Lock()

    @classmethod
    async def subscribe(cls, session) -> list[dict]:
        """Yield fresh player positions every tick, stopping when the caller
        breaks out of the loop (e.g. WebSocket disconnect or stream ends).
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        async with cls._lock:
            cls._subs.add(queue)
            if cls._task is None or cls._task.done():
                cls._task = asyncio.create_task(cls._run(session))
        try:
            while True:
                yield await queue.get()
        finally:
            async with cls._lock:
                cls._subs.discard(queue)
                if not cls._subs and cls._task is not None:
                    cls._task.cancel()
                    cls._task = None

    @classmethod
    async def _run(cls, session) -> None:
        while True:
            try:
                raw = await get_players_mod(session)
                wanted_ids, police_ids, costume_ids = await _get_hidden_player_unique_ids()
                any_wanted = bool(wanted_ids)
                processed = []
                for p in raw:
                    loc = p.get("Location", {})
                    try:
                        uid = int(p.get("UniqueID", 0))
                    except (ValueError, TypeError):
                        uid = 0
                    hidden = _should_hide_player(
                        p, wanted_ids, police_ids, costume_ids, any_wanted
                    )
                    processed.append(
                        {
                            "unique_id": uid,
                            "player_name": p.get("PlayerName", ""),
                            "x": 0.0 if hidden else float(loc.get("X", 0)),
                            "y": 0.0 if hidden else float(loc.get("Y", 0)),
                            "z": 0.0 if hidden else float(loc.get("Z", 0)),
                            "hidden": hidden,
                            "vehicle_key": p.get("VehicleKey", ""),
                        }
                    )
                for q in list(cls._subs):
                    try:
                        q.put_nowait(processed)
                    except asyncio.QueueFull:
                        pass  # subscriber is slow; drop stale frame
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in PlayerPositionsSubscription loop")
            await asyncio.sleep(POSITION_UPDATE_SLEEP)
