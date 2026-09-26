"""Illegal-race police trigger (Yuuka 2026-09-26).

Wires a TT-classed event's start transition into the cops-and-criminals
RP loop:

1. On start every racer gets the wanted star via
   :func:`amc.criminals.create_or_refresh_wanted` (system-trigger path —
   suspect flag in-game, "You are wanted" message, bounty = 10% criminal
   score, same as illicit-cargo triggers).  Wanted suspects are hidden
   from the map, which is what forces the chase to start from the event
   route (point 3 — /events already shows active events + route).
2. 60s into the race a global announcement fires:
   "An Illegal race is happening! Check Events!" — but only if the event
   is still live and still racing (state 2), so abandoned/finished runs
   stay silent.

The DQ check (handlers/tt_dq.py) runs FIRST in the same start reconcile;
players disqualified at the line are not marked wanted — they never
entered the race.
"""

from __future__ import annotations

import asyncio
import logging

from amc.criminals import create_or_refresh_wanted
from amc.mod_server import get_events, send_system_message
from amc.models import Character

logger = logging.getLogger(__name__)

RACE_ALERT_DELAY_SECONDS = 60
RACE_ALERT_MESSAGE = "An Illegal race is happening! Check Events!"


async def mark_racers_wanted(
    http_client_mod, game_event, live_event: dict, disqualified: list[str]
) -> list[str]:
    """Give every non-disqualified participant the wanted star.

    Returns the list of player names successfully marked (for logs/tests).
    Per-player failures are contained — one broken character row must not
    stop the rest of the lineup from being flagged.
    """
    marked: list[str] = []
    for player_info in live_event.get("Players", []):
        guid = (player_info.get("CharacterId") or {}).get("CharacterGuid", "")
        player_name = player_info.get("PlayerName", "") or guid[:8] or "unknown"
        if not guid or player_name in disqualified:
            continue
        try:
            character = await Character.objects.filter(guid=guid).afirst()
            if character is None:
                logger.info(
                    "TT race wanted-skip for %s in %s: no Character row",
                    player_name, game_event.guid,
                )
                continue
            await create_or_refresh_wanted(character, http_client_mod)
            marked.append(player_name)
            logger.info(
                "TT race start: %s (%s) marked wanted for %s",
                player_name, guid, game_event.guid,
            )
        except Exception:
            logger.warning(
                "TT race wanted failed for %s in %s",
                player_name, game_event.guid, exc_info=True,
            )
    return marked


async def announce_illegal_race(http_client_mod, game_event) -> None:
    """Schedule the 60s 'illegal race' global announcement.

    Fire-and-forget: sleeps RACE_ALERT_DELAY_SECONDS, re-checks the event
    is still live AND still racing (state 2 — the game resets TT events
    to state 1 between runs, and a vanished event must not announce),
    then broadcasts the alert.  All failures contained.
    """

    async def _alert() -> None:
        try:
            await asyncio.sleep(RACE_ALERT_DELAY_SECONDS)
            events = await get_events(http_client_mod)
            data = events.get("data", [])
            live = (
                data.values() if isinstance(data, dict) else data
            ) or []
            if not any(
                ev.get("EventGuid") == game_event.guid
                and ev.get("State") == 2
                for ev in live
            ):
                return
            await send_system_message(http_client_mod, RACE_ALERT_MESSAGE)
            logger.info(
                "TT race alert sent for %s (%s)",
                game_event.guid, game_event.name,
            )
        except Exception:
            logger.warning(
                "TT race alert failed for %s", game_event.guid, exc_info=True
            )

    asyncio.create_task(_alert())
