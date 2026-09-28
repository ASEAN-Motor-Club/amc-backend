"""Illegal-race police trigger (Yuuka 2026-09-26).

Wires a TT-classed event's start transition into the cops-and-criminals
RP loop:

1. On start every racer gets the suspect BADGE ONLY — no wanted star
   (Yuuka 2026-09-27: "badge should still exist, but no star"). The
   Wanted row is what feeds the wanted-tick anti-abuse despawn of modded
   vehicles, so none is created: no stars, no bounty, no "You are
   wanted" message — just the vanilla suspect GE via make_suspect.
2. Between 50-100% of the race's checkpoint completion (rolled per run)
   a global announcement fires: "An Illegal race is happening! Check
   Events!" — but only if the event is still live and still racing
   (state 2), so abandoned/finished runs stay silent.

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

RACE_ALERT_DELAY_SECONDS = 60  # fallback time gate when waypoints are unknown
RACE_ALERT_MESSAGE = "An Illegal race is happening! Check Events!"
# Varying trigger (Yuuka 2026-09-28): announce between 50-100% of the
# race's checkpoint completion instead of a flat 60 s. The fraction is
# rolled once per run and read live from the payload's per-player
# SectionIndex over the route's waypoint count (prod-verified 2026-09-28:
# SectionIndex 34/45 waypoints = the observed 75.6%).
RACE_ALERT_MIN_FRACTION = 0.5
RACE_ALERT_MAX_FRACTION = 1.0
RACE_ALERT_CHECK_INTERVAL = 15  # hook-task progress poll cadence
RACE_ALERT_MAX_CHECKS = 40  # give the task 10 min; the 30s tick backstops
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
# Per-run rolled trigger index (event guid -> waypoint index). Rolled on
# the first racing sighting of a run; reset in cancel_pending_race_alert
# and on the non-racing re-arm below so every run gets a fresh roll.
_alert_targets: dict[str, int | None] = {}


def _roll_target(live_event: dict) -> int | None:
    """Roll this run's trigger index from the live payload, or None when
    the route has no waypoint data (caller falls back to the 60 s gate)."""
    import random
    import math

    try:
        waypoints = live_event["RaceSetup"]["Route"]["Waypoints"]
        total = len(waypoints)
    except (KeyError, TypeError):
        return None
    if total <= 0:
        return None
    frac = random.uniform(RACE_ALERT_MIN_FRACTION, RACE_ALERT_MAX_FRACTION)
    # Clamp to total-1: SectionIndex never reports the route length, so a
    # 100% roll must still be reachable at the final waypoint.
    return min(total - 1, max(1, math.ceil(frac * total)))


def _progress_gate(guid: str, live_event: dict) -> bool:
    """True when the run has reached its rolled trigger (or the 60 s
    fallback when no waypoint data exists)."""
    target = _alert_targets.get(guid)
    if target is None:
        target = _alert_targets.setdefault(guid, _roll_target(live_event))
    if target is None:
        return _arm_or_gate(guid)
    best = -1
    for player in live_event.get("Players") or []:
        idx = player.get("SectionIndex", -1)
        if isinstance(idx, (int, float)) and idx > best:
            best = int(idx)
    return best >= target


def _arm_or_gate(guid: str) -> bool:
    """Fallback: track racing duration; True only once the event has
    raced >= RACE_ALERT_DELAY_SECONDS."""
    import time as _time

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
        match = next((ev for ev in live if ev.get("EventGuid") == guid), None)
        if match is None or match.get("State") != 2:
            # Re-arm: finished/reset/vanished — forget the guid AND this
            # run's rolled target so the next run rolls fresh.
            _announced_race_guids.discard(guid)
            _race_first_seen.pop(guid, None)
            _alert_targets.pop(guid, None)
            return False
        # Yuuka 2026-09-28: trigger between 50-100% of the route's
        # checkpoints passed (SectionIndex / waypoint count), rolled once
        # per run; falls back to the 60 s gate when the payload carries
        # no waypoint data.
        if not _progress_gate(guid, match):
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
    _alert_targets.pop(event_guid, None)


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
            # Poll the progress gate instead of sleeping out a flat 60 s:
            # the announcement lands on the first check where the run has
            # reached its rolled checkpoint fraction (or the 60 s fallback
            # when the route has no waypoints). The 30s suspect-tick race
            # pass calls ensure_announced too, so a given-up task never
            # leaves a race unannounced.
            for _ in range(RACE_ALERT_MAX_CHECKS):
                await asyncio.sleep(RACE_ALERT_CHECK_INTERVAL)
                if not await ensure_announced(http_client_mod, game_event):
                    continue
                break
            else:
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
