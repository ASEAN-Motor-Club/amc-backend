"""Single shared event loop for player-position streaming.

One background task polls the mod server once per tick and fans the snapshot
out to every subscriber (SSE position stream, SSE count stream, WebSocket).
Previously each SSE/WS connection ran its OWN poll loop against
get_players_mod, so concurrent connections (and multiple uvicorn workers)
fetched independently and could observe different rosters — duplicated /
stale entries between the cache TTL and the stream loops. With the
broadcaster there is exactly ONE fetch per tick per process and every
subscriber sees the same snapshot generation.
"""

import asyncio
import logging
import time

import aiohttp
from django.conf import settings

from amc.api.player_positions_common import (
    HEARTBEAT_INTERVAL,
    POSITION_UPDATE_SLEEP,
    get_players_mod_masked,
)

logger = logging.getLogger(__name__)


async def _default_fetch(session):
    # Bypass the mod-players cache: its TTL (2 s) is longer than the tick
    # interval (1 s), so cached reads would replay the previous snapshot and
    # emit duplicate position frames with fresh timestamps.
    return await get_players_mod_masked(session, use_cache=False)


class PositionsBroadcaster:
    def __init__(self, fetch=None, sleep_s=POSITION_UPDATE_SLEEP):
        self._fetch = fetch or _default_fetch
        self._sleep_s = sleep_s
        self._cond = asyncio.Condition()
        self._start_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._snapshot: list[dict] = []
        self._count = 0
        self._seq = 0
        self._ts = 0.0

    async def ensure_started(self):
        async with self._start_lock:
            if self._task is None or self._task.done():
                self._session = aiohttp.ClientSession(
                    base_url=settings.MOD_SERVER_API_URL
                )
                self._task = asyncio.create_task(self._run())

    async def _run(self):
        assert self._session is not None
        try:
            # Absolute schedule: each tick targets `last_deadline + interval`,
            # not `now + interval` after the work finished. Sleeping
            # relative to the END of the previous tick let the period drift
            # to fetch_duration + interval (e.g. 1.2 s -> 2.4 -> 3.6...).
            loop = asyncio.get_running_loop()
            deadline = loop.time()
            while True:
                try:
                    await self._tick()
                except Exception:
                    # Keep serving the last good snapshot; do not kill the loop.
                    logger.exception("positions broadcaster tick failed")
                deadline += self._sleep_s
                delay = deadline - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    # We are behind schedule (tick overran); do not skip ahead
                    # multiple intervals — re-anchor to now.
                    deadline = loop.time() + self._sleep_s
        finally:
            if self._session is not None:
                await self._session.close()
                self._session = None

    async def _tick(self):
        players = await self._fetch(self._session)
        if players is None:
            players = []
        # player_count semantics: hidden players are not counted.
        self._count = sum(1 for p in players if not p.get("hidden", False))
        self._snapshot = players
        self._ts = time.time()
        self._seq += 1
        async with self._cond:
            self._cond.notify_all()

    async def _wait_for_next(self, seq: int):
        """Block until a snapshot newer than `seq` exists; return (seq, snapshot)."""
        async with self._cond:
            while self._seq == seq:
                await self._cond.wait()
            return self._seq, self._snapshot, self._ts

    async def stream_masked(self):
        """Yield (masked_roster, queried_at_epoch_seconds) once per tick, in
        order, to every subscriber from the same generation. Consumers must
        not mutate the yielded list."""
        await self.ensure_started()
        seq = 0
        while True:
            seq, snapshot, ts = await self._wait_for_next(seq)
            yield snapshot, ts

    async def stream_count(self):
        """Count stream: yield only on change, heartbeats while stable."""
        await self.ensure_started()
        seq = 0
        last_count = None
        ticks_since_heartbeat = 0
        while True:
            seq, _, _ = await self._wait_for_next(seq)
            count = self._count
            if count != last_count:
                yield f"data: {count}\n\n"
                last_count = count
                ticks_since_heartbeat = 0
            else:
                ticks_since_heartbeat += 1
                if ticks_since_heartbeat * POSITION_UPDATE_SLEEP >= HEARTBEAT_INTERVAL:
                    yield ": heartbeat\n\n"
                    ticks_since_heartbeat = 0


_broadcaster: PositionsBroadcaster | None = None


def get_positions_broadcaster() -> PositionsBroadcaster:
    global _broadcaster
    if _broadcaster is None:
        _broadcaster = PositionsBroadcaster()
    return _broadcaster
