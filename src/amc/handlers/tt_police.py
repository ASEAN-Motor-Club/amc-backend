"""Illegal-race police trigger (Yuuka 2026-09-26).

Wires a TT-classed event's start transition into the cops-and-criminals
RP loop:

1. On start every racer gets the suspect BADGE ONLY — no wanted star
   (Yuuka 2026-09-27: "badge should still exist, but no star"). The
   Wanted row is what feeds the wanted-tick anti-abuse despawn of modded
   vehicles, so none is created: no stars, no bounty, no "You are
   wanted" message — just the vanilla suspect GE via make_suspect.
2. 60s into the race a global announcement fires:
   "An Illegal race is happening! Check Events!" — but only if the event
   is still live and still racing (state 2), so abandoned/finished runs
   stay silent.

The DQ check (handlers/tt_dq.py) runs FIRST in the same start reconcile;
disqualified players are not flagged — they never entered the race.
"""

from __future__ import annotations

import asyncio
import logging

from amc.mod_server import (
    broadcast_server_message,
    get_events,
)
logger = logging.getLogger(__name__)

RACE_ALERT_DELAY_SECONDS = 60
RACE_ALERT_MESSAGE = "An Illegal race is happening! Check Events!"
_alert_tasks: dict[str, asyncio.Task] = {}
_announced_race_guids: set[str] = set()
# Serializes the ensure_announced decide-and-mark section per guid: the SSE
# alert task (fires at t+60s) and the 30s suspect-tick race pass both call
# it, and the check-and-add spans awaits — without the lock both paths can
# pass the dedup concurrently and the race gets TWO announcements + two
# Wanted grants (Yuuka 2026-09-28, triple-alert report).
_announce_locks: dict[str, asyncio.Lock] = {}
# First sighting of the event RACING (monotonic). The announcement + star
# Wanted land only after RACE_ALERT_DELAY_SECONDS of actual racing
# (Yuuka 2026-09-27: "triggered immediately, not after 60 seconds").
_race_first_seen: dict[str, float] = {}


def _arm_or_gate(game_event) -> bool:
    """Track racing duration; True only once the event has raced >= 60 s."""
    import time as _time

    guid = game_event.guid
    now = _time.monotonic()
    first = _race_first_seen.setdefault(guid, now)
    if now - first < RACE_ALERT_DELAY_SECONDS:
        return False
    _race_first_seen.pop(guid, None)
    return True


async def ensure_announced(http_client_mod, game_event) -> bool:
    """True exactly once per event guid — the caller sends the broadcast.

    Shared de-dup between the SSE-hook alert task and the
    refresh_suspect_tags race pass (Yuuka 2026-09-27: the announcement
    must fire even when the hook path loses the race to a worker
    restart). Race events loop back to state 1 between runs, so a guid
    is re-armed whenever the event is seen non-racing.
    """
    guid = game_event.guid
    lock = _announce_locks.setdefault(guid, asyncio.Lock())
    async with lock:
        if guid in _announced_race_guids:
            return False
        # Re-arm: if the event is no longer racing (finished/reset), forget
        # it. get_events returns the EVENT LIST (or None) — NOT a
        # {"data": ...} dict.
        events = await get_events(http_client_mod)
        live = events or []
        racing = any(
            ev.get("EventGuid") == guid and ev.get("State") == 2 for ev in live
        )
        if not racing:
            _announced_race_guids.discard(guid)
            _race_first_seen.pop(guid, None)
            return False
        # 60 s of actual racing before the announcement + stars land.
        if not _arm_or_gate(game_event):
            return False
        _announced_race_guids.add(guid)
        return True


async def grant_race_wanted(http_client_mod, game_event) -> list[str]:
    """Grant real Wanted (stars) to every online participant of the event.

    Yuuka 2026-09-27 rework: at race start nobody is flagged; ~60 s after
    start the announcement fires and ALL players inside the event get a
    real Wanted row (the star status) — origin 'event_race',
    mod_vehicles_allowed=True (the wanted-tick despawn pass skips them),
    bounty 0 (flag-only; race enforcement is not a confiscation source).

    The refresh loop in refresh_suspect_tags keeps topping the countdown
    up every 30 s until the race finishes / the player leaves / the
    event ends; after that the normal speed-law decay takes over
    (the star decays naturally — no forced clear).
    """
    granted: list[str] = []
    from amc.criminals import (
        WANTED_ORIGIN_EVENT_RACE,
        create_or_refresh_wanted,
    )

    async for participant in game_event.participants.select_related(
        "character"
    ).all():
        char = participant.character
        if not char or not char.guid:
            continue
        if not (char.last_online and char.last_online >= online_cutoff_dt()):
            continue
        try:
            await create_or_refresh_wanted(
                char,
                http_client_mod,
                origin=WANTED_ORIGIN_EVENT_RACE,
                mod_vehicles_allowed=True,
                bounty=0,
            )
            granted.append(char.guid)
        except Exception:
            logger.warning(
                "race wanted grant failed for %s", char.name, exc_info=True
            )
    return granted


def online_cutoff_dt():
    """Online = seen in the last 90 s (matches the wanted pass cutoff)."""
    from django.utils import timezone
    from datetime import timedelta

    return timezone.now() - timedelta(seconds=90)


def cancel_pending_race_alert(event_guid: str) -> None:
    """Drop the pending 60s alert task + racing gate for ``event_guid``.

    Called on every state transition that is NOT into racing (finish /
    between-run reset / removal) so a sleeper armed by the previous run
    cannot fire into the next one, and on re-arm (one timer per guid —
    Yuuka 2026-09-28: stacked sleepers fired 3 announcements on a re-run
    setup, and a Start→Ready→Start toggle announced off the FIRST start).
    """
    task = _alert_tasks.pop(event_guid, None)
    if task is not None and not task.done():
        task.cancel()
    _race_first_seen.pop(event_guid, None)


async def announce_illegal_race(
    http_client_mod, game_event, http_client_game=None
) -> None:
    """Broadcast the 60s 'illegal race' global announcement.

    ONE pending timer per event guid: re-arming cancels the previous
    sleeper (Yuuka 2026-09-28 — stacked sleepers from repeated starts
    each fired an announcement; Start→Ready→Start announced off the
    first start). At t+60s the task asks ensure_announced (exactly-once
    per guid, shared with the 30s suspect-tick race pass) whether the
    event has really raced ≥60s and still lives — a silent return there
    is fine, the tick pass is the restart/timing backstop. All failures
    contained; the task handle is held in a module-level registry so it
    cannot be garbage collected mid-flight.
    """
    cancel_pending_race_alert(game_event.guid)

    async def _alert() -> None:
        try:
            await asyncio.sleep(RACE_ALERT_DELAY_SECONDS)
            if not await ensure_announced(http_client_mod, game_event):
                return
            await broadcast_server_message(
                http_client_game, RACE_ALERT_MESSAGE
            )
            # Yuuka 2026-09-27 rework: the announcement IS the moment the
            # star Wanted lands on everyone inside the event.
            await grant_race_wanted(http_client_mod, game_event)
            logger.info(
                "TT race alert sent for %s (%s)",
                game_event.guid, game_event.name,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "TT race alert failed for %s", game_event.guid, exc_info=True
            )

    guid = game_event.guid
    task = asyncio.create_task(_alert())
    _alert_tasks[guid] = task
    def _done(t: asyncio.Task, _guid: str = guid) -> None:
        if _alert_tasks.get(_guid) is t:
            _alert_tasks.pop(_guid, None)

    task.add_done_callback(_done)
