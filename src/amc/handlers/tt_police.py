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

from amc.mod_server import get_events, make_suspect, send_system_message
from amc.models import Character

logger = logging.getLogger(__name__)

RACE_ALERT_DELAY_SECONDS = 60
RACE_ALERT_MESSAGE = "An Illegal race is happening! Check Events!"
_ALERT_MAX_POLLS = 10  # announce on the first tick where the event races
_alert_tasks: set[asyncio.Task] = set()


async def mark_racers_wanted(
    http_client_mod, game_event, live_event: dict, disqualified: list[str]
) -> list[str]:
    """Flag every non-disqualified participant as an illegal racer.

    Badge WITHOUT the wanted star (Yuuka 2026-09-27: "badge should still
    exist, but no star"). The suspect gameplay effect alone is safe — the
    despawn risk comes from the Wanted row (stars) feeding the wanted-tick
    anti-abuse. So: make_suspect only, no Wanted row, no bounty, no
    "You are wanted" message. The start-line DQ remains enforcement.
    """
    flagged: list[str] = []
    for player_info in live_event.get("Players", []):
        guid = (player_info.get("CharacterId") or {}).get("CharacterGuid", "")
        player_name = player_info.get("PlayerName", "") or guid[:8] or "unknown"
        if not guid or player_name in disqualified:
            continue
        try:
            character = await Character.objects.filter(guid=guid).afirst()
            if character is None:
                logger.info(
                    "TT race flag-skip for %s in %s: no Character row",
                    player_name, game_event.guid,
                )
                continue
            await make_suspect(http_client_mod, guid)
            flagged.append(player_name)
            logger.info(
                "TT race start: %s (%s) flagged illegal racer (badge, no stars) for %s",
                player_name, guid, game_event.guid,
            )
        except Exception:
            logger.warning(
                "TT race flag failed for %s in %s",
                player_name, game_event.guid, exc_info=True,
            )
    return flagged


async def announce_illegal_race(http_client_mod, game_event) -> None:
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
                events = await get_events(http_client_mod)
                data = events.get("data", [])
                live = (
                    data.values() if isinstance(data, dict) else data
                ) or []
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
                await send_system_message(http_client_mod, RACE_ALERT_MESSAGE)
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
