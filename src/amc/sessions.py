"""Session-range integrity for :class:`amc.models.PlayerStatusLog`.

A login opens a range, a logout closes it. The log stream feeding those two
events is not reliable: a dedicated-server restart, a RELP outage or a handler
failure drops lines. Two failure shapes followed from that:

* a logout that never arrived left the range open forever (``upper_inf``), and
* a later logout that *did* arrive matched the newest range containing its
  timestamp — sometimes a range opened weeks earlier — and stretched it into a
  multi-week "session".

Both end up in play-time totals. The rules enforced here:

* a range is never closed at a boundary further than ``MAX_SESSION`` from its
  start unless :class:`amc.models.ServerLog` proves when the session really
  ended (a login/logout line for that player inside the span), or the
  character's own activity log gives a lower bound for the end;
* when there is no proof at all the range is left open. An open range
  contributes no duration, which is wrong in a harmless direction; inventing
  hours is not.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import NamedTuple

from django.db.models import Q
from psycopg.types.range import Range

from amc.models import (
    Character,
    PlayerChatLog,
    PlayerRestockDepotLog,
    PlayerStatusLog,
    PlayerVehicleLog,
    ServerLog,
)

logger = logging.getLogger("amc.sessions")

#: Longest plausible single play session. Anything above this is a symptom of
#: a lost line, not a marathon — the observed legitimate maximum is ~11 h.
MAX_SESSION = timedelta(hours=12)

LOGIN_PREFIX = "Player Login: "
LOGOUT_PREFIX = "Player Logout: "
RESTART_PREFIX = "DedicatedServer is started"

#: Restart boundaries close sessions before logouts open new ones at the same
#: instant; ordering is part of the rebuild semantics.
_KIND_ORDER = {"restart": 0, "logout": 1, "login": 2}


class SessionEvent(NamedTuple):
    """One line of the session stream, already resolved to a character."""

    timestamp: datetime
    kind: str  # "login" | "logout" | "restart"
    character_id: int | None = None  # None for restarts
    host: str = ""


def build_session_ranges(
    events: list[SessionEvent],
) -> list[tuple[int, datetime, datetime | None, str]]:
    """Derive ``(character_id, start, end, host)`` ranges from raw events.

    Pure function: no DB access, so the pairing rules are unit-testable. An
    unclosed session is emitted with ``end=None`` (an open range).
    """
    open_sessions: dict[int, tuple[datetime, str]] = {}
    ranges: list[tuple[int, datetime, datetime | None, str]] = []

    def close(character_id: int, at: datetime) -> None:
        started = open_sessions.pop(character_id, None)
        if started is None:
            return
        start, host = started
        if at > start:
            ranges.append((character_id, start, at, host))

    for event in sorted(events, key=lambda e: (e.timestamp, _KIND_ORDER[e.kind])):
        if event.kind == "restart":
            for character_id, (_start, host) in list(open_sessions.items()):
                # A restart of one host says nothing about the others; legacy
                # rows without a host are treated as belonging to whichever
                # server restarted.
                if host == event.host or not host:
                    close(character_id, event.timestamp)
            continue
        if event.character_id is None:
            continue
        if event.kind == "login":
            close(event.character_id, event.timestamp)
            open_sessions[event.character_id] = (event.timestamp, event.host)
        else:  # logout
            close(event.character_id, event.timestamp)

    for character_id, (start, host) in open_sessions.items():
        ranges.append((character_id, start, None, host))

    ranges.sort(key=lambda row: (row[1], row[0]))
    return ranges


async def _player_unique_id(character_id: int) -> int | None:
    return (
        await Character.objects.filter(pk=character_id)
        .values_list("player__unique_id", flat=True)
        .afirst()
    )


async def _log_boundary(
    character_id: int, start: datetime, boundary: datetime
) -> datetime | None:
    """Earliest login/logout log line for this player inside (start, boundary).

    A logout line is the exact end of the session; a login line is the end of
    the previous one (the player had to be gone to log in again). Either way it
    is proof the session did not continue past that timestamp.
    """
    unique_id = await _player_unique_id(character_id)
    if unique_id is None:
        return None
    line = (
        await ServerLog.objects.filter(
            timestamp__gt=start,
            timestamp__lt=boundary,
            text__contains=f"({unique_id})",
        )
        .filter(Q(text__startswith=LOGIN_PREFIX) | Q(text__startswith=LOGOUT_PREFIX))
        .order_by("timestamp")
        .afirst()
    )
    return line.timestamp if line else None


async def activity_boundary(
    character_id: int, start: datetime, boundary: datetime
) -> datetime | None:
    """Latest in-game activity timestamp inside the span, if any.

    Weaker than a log line (an idle player produces none) but never invents
    time: the player provably was in the world at that moment.
    """
    latest: datetime | None = None
    for model in (PlayerVehicleLog, PlayerChatLog, PlayerRestockDepotLog):
        ts = (
            await model.objects.filter(
                character_id=character_id,
                timestamp__gt=start,
                timestamp__lt=boundary,
            )
            .order_by("-timestamp")
            .values_list("timestamp", flat=True)
            .afirst()
        )
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest


async def session_end(
    character_id: int, start: datetime, boundary: datetime
) -> datetime | None:
    """Best evidence-backed end for a session that started at ``start``.

    ``None`` means "no proof — leave the range open".
    """
    if boundary - start <= MAX_SESSION:
        return boundary
    proof = await _log_boundary(character_id, start, boundary)
    if proof is not None:
        return proof
    return await activity_boundary(character_id, start, boundary)


async def close_open_sessions(
    character_id: int, boundary: datetime, reason: str = ""
) -> list[tuple[int, datetime, datetime]]:
    """Close open sessions of one character at or before ``boundary``.

    Returns the ``(pk, start, end)`` of every range that was closed. Ranges
    with no evidence for their end are left open and reported.
    """
    closed: list[tuple[int, datetime, datetime]] = []
    skipped = 0
    async for row in PlayerStatusLog.objects.filter(
        character_id=character_id, timespan__upper_inf=True
    ):
        start = row.timespan.lower
        if start is None or start >= boundary:
            continue
        end = await session_end(character_id, start, boundary)
        if end is None:
            skipped += 1
            continue
        await PlayerStatusLog.objects.filter(pk=row.pk).aupdate(
            timespan=Range(start, end, bounds="[)")
        )
        closed.append((row.pk, start, end))
    if closed:
        logger.info(
            "Closed %d open session(s) for character %s at %s (%s)",
            len(closed),
            character_id,
            boundary.isoformat(),
            reason or "unspecified",
        )
    if skipped:
        logger.error(
            "Left %d untrustworthy open session(s) for character %s: no evidence "
            "of their end (run `manage.py repair_player_sessions --apply`)",
            skipped,
            character_id,
        )
    return closed


async def close_sessions_on_restart(hostname: str, timestamp: datetime) -> int:
    """Close the sessions a restart of ``hostname`` interrupted.

    Only sessions that started within ``MAX_SESSION`` of the restart are closed
    here — those are the ones that were genuinely live when the server went
    down. Older open ranges are left alone: closing them at the restart is
    exactly the bug this module exists to prevent. ``repair_player_sessions``
    is the tool for those.

    Only rows tagged with this ``hostname`` are touched, so an event-server
    restart cannot truncate sessions on the main server. Legacy rows (empty
    host) are skipped for the same reason and are replaced by the rebuild.
    """
    closed = 0
    stale = 0
    async for row in PlayerStatusLog.objects.filter(
        timespan__upper_inf=True, host=hostname
    ):
        start = row.timespan.lower
        if start is None or start >= timestamp:
            continue
        if timestamp - start > MAX_SESSION:
            stale += 1
            continue
        await PlayerStatusLog.objects.filter(pk=row.pk).aupdate(
            timespan=Range(start, timestamp, bounds="[)")
        )
        closed += 1
    if closed:
        logger.info(
            "Closed %d session(s) interrupted by the %s restart", closed, hostname
        )
    if stale:
        logger.error(
            "Left %d stale open session(s) untouched on %s restart: closing them "
            "at the restart timestamp would invent hours (run "
            "`manage.py repair_player_sessions --apply`)",
            stale,
            hostname,
        )
    return closed
