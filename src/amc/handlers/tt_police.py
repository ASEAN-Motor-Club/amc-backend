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
_ALERT_MAX_POLLS = 10  # announce on the first tick where the event races
_alert_tasks: set[asyncio.Task] = set()
_announced_race_guids: set[str] = set()
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
    if guid in _announced_race_guids:
        return False
    # Re-arm: if the event is no longer racing (finished/reset), forget it.
    # get_events returns the EVENT LIST (or None) — NOT a {"data": ...} dict.
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


async def announce_illegal_race(
    http_client_mod, game_event, http_client_game=None
) -> None:
    """Broadcast the 60s 'illegal race' global announcement.

    Polls every RACE_ALERT_DELAY_SECONDS until the event is seen racing
    (state 2 — the game resets TT events to state 1 between runs and the
    hook-time snapshot can be stale), announces once, then stops. Gives
    up after _ALERT_MAX_POLLS or when the event vanishes. All failures
    contained. The task handle is held in a module-level set —
    fire-and-forget tasks without a reference can be garbage collected
    mid-flight (Yuuka 2026-09-27: "I didn't see any announcement at all").
    """

    async def _alert() -> None:
        try:
            for _ in range(_ALERT_MAX_POLLS):
                await asyncio.sleep(RACE_ALERT_DELAY_SECONDS)
                # get_events returns the EVENT LIST (or None) — not a
                # {"data": ...} dict.
                live = await get_events(http_client_mod) or []
                match = next(
                    (
                        ev
                        for ev in live
                        if ev.get("EventGuid") == game_event.guid
                    ),
                    None,
                )
                if match is None:
                    return
                if match.get("State") != 2:
                    continue
                await broadcast_server_message(
                    http_client_game, RACE_ALERT_MESSAGE
                )
                _announced_race_guids.add(game_event.guid)
                # Yuuka 2026-09-27 rework: the announcement IS the moment the
                # star Wanted lands on everyone inside the event.
                await grant_race_wanted(http_client_mod, game_event)
                logger.info(
                    "TT race alert sent for %s (%s)",
                    game_event.guid, game_event.name,
                )
                return
        except Exception:
            logger.warning(
                "TT race alert failed for %s", game_event.guid, exc_info=True
            )

    _alert_tasks.add(asyncio.create_task(_alert()))
