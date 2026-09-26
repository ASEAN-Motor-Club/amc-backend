"""Time-trial start-line disqualification (Yuuka 2026-09-26: "discord audit
only.. I don't like that, do we have a way to custom 'disqualify' them like
how the game does?").

At the 1→2 start transition of a TT-classed event, every participant's
parts are evaluated against the event's class rules
(:func:`amc.tt_rules.evaluate_tt_parts` — engine ≤ max_hp + buffer, vanilla
tires).  Violators are force-removed through the mod's
``POST /events/{guid}/leave`` endpoint, which drives the *same native*
``ServerLeaveEvent`` RPC the game uses for a voluntary leave — the vanilla
"disqualified" outcome without touching vanilla restrictions.  A DQ card
with the specific violations is posted to the parts channel
(``DISCORD_PARTS_LOG_CHANNEL_ID``).

Design constraints carried from the review:

* Fires ONLY on the start transition (the require_state==2 reconcile) —
  never while players are preparing (Yuuka: kicked "when the event
  starts… so they don't get kicked when preparing").  The game resets TT
  events to state 1 between runs, so each new run's start re-checks
  everyone.
* Joins only happen pre-race (state 1), so no re-entry loop exists at
  state 2 (Yuuka confirmed: "Players can't join a started event").
* A kicked player's never-raced row is deleted so results don't render a
  phantom DNF.
* Every failure path is contained: a dead mod endpoint, a missing parts
  payload, or a Discord hiccup must never break the start reconcile.
* No wanted status is set — enforcement is the DQ kick + Discord record.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from django.conf import settings

from amc.mod_server import (
    get_player_last_vehicle,
    get_player_last_vehicle_parts,
    kick_player_from_event,
)
from amc.models import GameEventCharacter, TTClass
from amc.parts_audit import _post_audit_embed
from amc.tt_rules import evaluate_tt_parts
from amc.vehicles import format_vehicle_name

logger = logging.getLogger(__name__)


async def _disqualify_illegal_starters(
    http_client_mod, game_event, live_event: dict, discord_client=None
) -> list[str]:
    """Evaluate + DQ every illegal participant at a TT event's start.

    *game_event* must have ``tt_class`` set for classed events (the caller
    passes the freshly upserted row; the FK is fetched on demand).
    *live_event* is the start-moment roster from the mod.

    Returns the list of disqualified player names (for logging/tests);
    empty when the event has no TT class or nobody violated.
    """
    if not game_event.tt_class_id:
        return []
    tt_class = await TTClass.objects.aget(pk=game_event.tt_class_id)
    max_hp = tt_class.max_hp
    event_guid = game_event.guid
    disqualified: list[str] = []

    for player_info in live_event.get("Players", []):
        character_id = player_info.get("CharacterId") or {}
        guid = character_id.get("CharacterGuid", "")
        # The leave endpoint keys on UniqueNetId ("PlayerId" in the mod's
        # HandleLeaveEvent); the event-player serialization carries it
        # inside CharacterId (MTDediMod Scripts/Helpers.lua:281).
        unique_net_id = character_id.get("UniqueNetId", "")
        player_name = player_info.get("PlayerName", "") or guid[:8] or "unknown"
        if not guid or not unique_net_id:
            continue
        try:
            last_vehicle, parts_data = await asyncio.gather(
                get_player_last_vehicle(http_client_mod, guid),
                get_player_last_vehicle_parts(http_client_mod, guid, complete=False),
            )
            vehicle = last_vehicle.get("vehicle")
            parts = parts_data.get("parts", [])
            if not vehicle or not parts:
                # Can't verify -> can't accuse.  Same no-fabrication rule as
                # the audit: skip rather than DQ on missing data.
                logger.info(
                    "TT start check skipped for %s in %s: no vehicle/parts data",
                    player_name, event_guid,
                )
                continue

            violations = evaluate_tt_parts(parts, max_hp)
            if not violations:
                continue

            # Force the vanilla leave path — the game's own DQ outcome.
            await kick_player_from_event(http_client_mod, event_guid, unique_net_id)
            disqualified.append(player_name)

            # Their never-raced row would render as a phantom DNF.
            await GameEventCharacter.objects.filter(
                game_event=game_event,
                character__guid=guid,
                finished=False,
                laps=0,
            ).adelete()

            embed = discord.Embed(
                title=f"DISQUALIFIED — {player_name}",
                description=(
                    f"Removed from **{game_event.name} [TT-{max_hp}]** at start"
                ),
                color=discord.Color.red(),
            )
            embed.add_field(
                name=format_vehicle_name(vehicle.get("fullName") or "") or "Vehicle",
                value="\n".join(f"- {v}" for v in violations),
                inline=False,
            )
            channel_id = int(settings.DISCORD_PARTS_LOG_CHANNEL_ID or 0)
            if channel_id and discord_client is not None:
                await _post_audit_embed(discord_client, channel_id, embed)
            logger.info(
                "TT DQ: %s (%s) kicked from %s — %s",
                player_name, guid, event_guid, "; ".join(violations),
            )
        except Exception:
            logger.warning(
                "TT start DQ failed for %s in %s", player_name, event_guid,
                exc_info=True,
            )
    return disqualified
