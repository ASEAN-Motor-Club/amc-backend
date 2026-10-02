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

RACE_ALERT_MESSAGE = "An Illegal race is happening! Check Events!"
# Varying trigger (Yuuka 2026-09-28): announce between 50-100% of the
# race's checkpoint completion. The fraction is
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
# alert task (15s progress polls) and the 30s suspect-tick race pass both call
# it, and the check-and-add spans awaits — without the lock both paths can
# pass the dedup concurrently and the race gets TWO announcements + two
# Wanted grants (Yuuka 2026-09-28, triple-alert report).
_announce_locks: dict[str, asyncio.Lock] = {}
# Per-run rolled trigger index (event guid -> waypoint index). Rolled on
# the first racing sighting of a run; reset in cancel_pending_race_alert
# and on the non-racing re-arm below so every run gets a fresh roll.
# No time gate anymore (Yuuka 2026-10-03): the 60s fallback made sub-1-minute
# races unannounceable — the race finished before the delay elapsed and the
# 30s tick only sees state-2 rows. A run with no readable waypoint data now
# announces on the first gate check (~15s), every time.
_alert_targets: dict[str, int | None] = {}


async def _roll_target(game_event, live_event: dict) -> int | None:
    """Roll this run's trigger index from the run's waypoint count, or
    None when no waypoint data exists (the caller then announces on the
    first gate check — no time fallback).

    Waypoint count comes from the DB race setup when the row has one
    (deterministic, survives payload quirks); the live payload is the
    fallback for rows created before a setup link landed.
    """
    import math
    import random

    total = None
    if game_event is not None and game_event.race_setup_id:
        from amc.models import RaceSetup

        setup = await RaceSetup.objects.aget(pk=game_event.race_setup_id)
        config = setup.config or {}
        total = len((config.get("Route", {}) or {}).get("Waypoints") or [])
    if not total:
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


async def _progress_gate(guid: str, live_event: dict, game_event=None) -> bool:
    """True when the run has reached its rolled trigger (or always, when
    no waypoint data exists — there is no time fallback).

    Progress source: the DB section stream (GameEventCharacter
    .section_index, maintained by ServerPassedRaceSection) merged with
    the live payload's Players[].SectionIndex — take whichever is
    further. The DB is the reliable one: the live list payload's
    SectionIndex has been observed not advancing for some events
    (prod 2026-10-02: OjiNorthTT-V1 reruns raced to section 49/49 with
    zero announcements — the live-payload gate never fired), while the
    section stream provably tracks every crossing.
    """
    target = _alert_targets.get(guid)
    if target is None:
        target = _alert_targets.setdefault(
            guid, await _roll_target(game_event, live_event)
        )
    if target is None:
        # No waypoint data at all: no time gate — the run announces on the
        # first gate check (~15s) every time.
        return True
    best = -1
    for player in live_event.get("Players") or []:
        idx = player.get("SectionIndex", -1)
        if isinstance(idx, (int, float)) and idx > best:
            best = int(idx)
    if game_event is not None and game_event.pk is not None:
        async for participant in game_event.participants.all():
            if participant.section_index > best:
                best = participant.section_index
    return best >= target


async def ensure_announced(http_client_mod, game_event) -> bool:
    """True exactly once per RUN — the caller sends the broadcast.

    Shared de-dup between the SSE-hook alert task and the
    refresh_suspect_tags race pass (Yuuka 2026-09-27: the announcement
    must fire even when the hook path loses the race to a worker
    restart). The marker is per-run: cancel_pending_race_alert clears
    it on every non-racing transition (Yuuka 2026-10-02 — reruns of the
    same guid re-announce and re-grant instead of staying silent until
    a worker restart), so within one run the marker is what keeps the
    task + tick paths from double-firing.
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
            _alert_targets.pop(guid, None)
            return False
        # Yuuka 2026-09-28: trigger between 50-100% of the route's
        # checkpoints passed (SectionIndex / waypoint count), rolled once
        # per run; with no waypoint data anywhere the gate passes on the
        # first check (no time fallback, Yuuka 2026-10-03). Progress reads
        # the DB section stream merged
        # with the live payload (2026-10-02: live-payload SectionIndex
        # can stall — the DB stream is the racing-truth source).
        if not await _progress_gate(guid, match, game_event):
            return False
        _announced_race_guids.add(guid)
        return True


async def grant_race_wanted(http_client_mod, game_event) -> list[str]:
    """Grant real Wanted (stars) to every online participant of the event.

    Yuuka 2026-09-27 rework: at race start nobody is flagged; at the
    announcement (checkpoint-progression gate, rolled 50-100% of the
    route per run) ALL players inside the event get a real Wanted row
    (the star status) — origin 'event_race', mod_vehicles_allowed=True
    (the wanted-tick despawn pass skips them), bounty 0 (flag-only; race
    enforcement is not a confiscation source).

    The badge is Schedule 1's wanted system's own make_suspect —
    create_or_refresh_wanted applies it for ALL origins including this
    one (Yuuka 2026-10-01; the 2026-09-30 "no badge for event wanted"
    exclusion was a misunderstanding and is removed). Stars are granted
    ONCE and decay through the standard wanted law (no top-up); a long
    race can expire its own wanted mid-run.
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
    from datetime import timedelta

    from django.utils import timezone

    return timezone.now() - timedelta(seconds=90)


def cancel_pending_race_alert(event_guid: str) -> None:
    """Drop the pending alert task for ``event_guid``.

    Called on every state transition that is NOT into racing (finish /
    between-run reset / removal) so a sleeper armed by the previous run
    cannot fire into the next one, and on re-arm (one timer per guid —
    Yuuka 2026-09-28: stacked sleepers fired 3 announcements on a re-run
    setup, and a Start→Ready→Start toggle announced off the FIRST start).

    Also clears the guid's announce marker: the announcement + star
    Wanted are per-RUN (Yuuka 2026-10-02: "restarted events would
    consistently give that wanted star, not just the first run" — the
    first OjiNorthTT run got the star, every rerun of the same guid was
    silent until the worker restarted). The old exactly-once-per-guid
    marker had no other re-arm path: the 30s tick race pass only
    iterates state-2 rows, so it can never observe a finished run.
    """
    task = _alert_tasks.pop(event_guid, None)
    if task is not None and not task.done():
        task.cancel()
    _announced_race_guids.discard(event_guid)
    _alert_targets.pop(event_guid, None)


async def announce_illegal_race(
    http_client_mod, game_event, http_client_game=None
) -> None:
    """Broadcast the 'illegal race' global announcement.

    ONE pending poller per event guid: re-arming cancels the previous
    one (Yuuka 2026-09-28 — stacked pollers from repeated starts
    each fired an announcement; Start→Ready→Start announced off the
    first start). Every 15s the poller asks ensure_announced
    (exactly-once per RUN, shared with the 30s suspect-tick race pass —
    Yuuka 2026-10-02: reruns re-arm via cancel_pending_race_alert)
    whether the run has reached its rolled checkpoint — no time gate
    (Yuuka 2026-10-03: the old 60s floor made sub-1-minute races
    unannounceable) — and still lives. A silent return there
    is fine, the tick pass is the restart/timing backstop. All failures
    contained; the task handle is held in a module-level registry so it
    cannot be garbage collected mid-flight.
    """
    cancel_pending_race_alert(game_event.guid)

    async def _alert() -> None:
        try:
            # Poll the progress gate — the announcement lands on the first
            # check where the run has reached its rolled checkpoint
            # fraction (immediately when the route has no waypoints). The
            # 30s suspect-tick race pass calls ensure_announced too, so a
            # given-up task never leaves a race unannounced.
            for _ in range(RACE_ALERT_MAX_CHECKS):
                await asyncio.sleep(RACE_ALERT_CHECK_INTERVAL)
                if not await ensure_announced(http_client_mod, game_event):
                    continue
                break
            else:
                return
            # Yuuka 2026-10-01 (prod: OjiNorthTT-V1 - IR - 480 announced
            # nothing): grant BEFORE the broadcast and CONTAIN the broadcast.
            # ensure_announced has already marked the guid exactly-once by
            # the time we're here — a broadcast exception used to kill the
            # task before grant_race_wanted ran, and the 30 s tick backstop
            # then saw the guid as announced and skipped the grant too, so
            # the whole race went star-less. The wanted grant is the action
            # that must not be skipped; the announcement is cosmetic.
            try:
                await grant_race_wanted(http_client_mod, game_event)
            except Exception:
                logger.warning(
                    "TT race wanted grant failed for %s",
                    game_event.guid, exc_info=True,
                )
            try:
                await broadcast_server_message(
                    http_client_game, RACE_ALERT_MESSAGE
                )
            except Exception:
                logger.warning(
                    "TT race broadcast failed for %s (grant already done)",
                    game_event.guid, exc_info=True,
                )
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
