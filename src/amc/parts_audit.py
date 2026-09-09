"""Silent parts audit — event-join logging + the /check_all_players command.

Shares the exact data path the in-game ``/check_parts`` popup uses: the mod's
minimal parts payload (``{ID, Key, Slot, Damage}``, ``Key`` == powercalc ids)
resolved through :func:`powercalc.vehicle_setup.compute_popup_lines`.  No
in-game popup is ever sent here — the report goes to Discord only
(``DISCORD_PARTS_LOG_CHANNEL_ID``; unset/0 disables posting entirely).

Report lines per player (the operator-requested subset):

* ``Power:`` — powercalc output (same ``Power: Unknown`` / no-fabrication
  degradation rules as the popup; omitted when the vehicle has no engine slot)
* ``Engine:`` / ``Intake:`` / ``Turbocharger:`` — the *installed* part keys
  (raw payload ids, not the model-resolved names), ``None`` when the slot is
  empty — that's the signal, not a fabrication
* ``Tires:`` — every Tire-slot key (slots 19..38 per ``VehiclePartSlot``)
"""

import asyncio
import logging

import discord
from django.conf import settings

from amc.mod_server import get_player_last_vehicle, get_player_last_vehicle_parts
from amc.vehicles import format_vehicle_name
from powercalc.vehicle_setup import compute_popup_lines

logger = logging.getLogger(__name__)

# VehiclePartSlot.Tire0..TireMax (amc/enums.py) — plain ints so this module
# can be exercised without importing the Django-side enum.
TIRE_SLOT_MIN = 19
TIRE_SLOT_MAX = 38

_SLOT_LABELS = {2: "Engine", 5: "Intake", 7: "Turbocharger"}


def summarize_parts(parts: list[dict]) -> list[str]:
    """Build the compact audit lines from a minimal parts payload.

    Never raises and never fabricates: an unresolvable engine yields exactly
    ``Power: Unknown`` (popup rule); an empty engine slot omits the Power
    line and reports ``Engine: None``.
    """
    by_slot: dict[int, str] = {}
    for part in parts:
        key = part.get("Key")
        if key:
            by_slot.setdefault(part.get("Slot", 0), key)

    lines: list[str] = []
    power_lines = compute_popup_lines(parts)
    if power_lines:
        # Line 0 is always the Power line; skip the popup's Intake/Turbo
        # model summary and the model/data-version footer — the explicit
        # installed-key lines below replace the former and the latter is
        # noise in a log channel.
        lines.append(power_lines[0])

    for slot in (2, 5, 7):
        label = _SLOT_LABELS[slot]
        lines.append(f"{label}: {by_slot.get(slot) or 'None'}")

    tires = [by_slot[s] for s in sorted(by_slot) if TIRE_SLOT_MIN <= s <= TIRE_SLOT_MAX]
    lines.append("Tires: " + (", ".join(tires) if tires else "None"))
    return lines


def _audit_embed(player_name: str, vehicle: dict, parts: list[dict], source: str) -> discord.Embed:
    lines = summarize_parts(parts)
    embed = discord.Embed(
        title=f"Parts Audit — {player_name}",
        description="\n".join(
            [f"**Vehicle:** {format_vehicle_name(vehicle['fullName'])}"] + lines
        ),
        color=0x3498DB,
    )
    embed.set_footer(text=f"source: {source}")
    return embed


async def _resolve_audit_channel(client):
    """Resolve the audit channel; never hangs (bounded ready-wait).

    Returns ``(channel, disabled)`` — ``disabled`` is True when the feature
    is OFF (channel id 0 or no client): callers treat that as "embed built,
    delivery intentionally skipped" rather than a failure.  A configured
    channel the bot cannot resolve (``get_channel`` None — private channel
    the bot has no View-Channel access to) is NOT "disabled": it's a
    misconfiguration the caller must surface.
    """
    channel_id = int(settings.DISCORD_PARTS_LOG_CHANNEL_ID or 0)
    if not channel_id or client is None:
        return None, True
    if not client.is_ready():
        try:
            await asyncio.wait_for(client.wait_until_ready(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("Parts audit skipped — Discord client not ready")
            return None, False
    return client.get_channel(channel_id), False


async def audit_character(
    http_client_mod,
    character_guid: str,
    player_name: str,
    discord_client=None,
    source: str = "event-join",
) -> discord.Embed | None:
    """Fetch one character's last vehicle + parts and post the audit embed.

    Returns the embed that was posted (or built when the feature is
    disabled) so callers can count successes; None when the fetch failed,
    there was nothing to audit, or the report could not be delivered.
    Failures never propagate — a dead mod endpoint or a missing channel
    must not break the join reconcile.
    """
    try:
        last_vehicle, parts_data = await asyncio.gather(
            get_player_last_vehicle(http_client_mod, character_guid),
            get_player_last_vehicle_parts(http_client_mod, character_guid, complete=False),
        )
    except Exception:
        logger.warning(
            "Parts audit fetch failed for %s (%s)", player_name, character_guid,
            exc_info=True,
        )
        return None

    vehicle = last_vehicle.get("vehicle")
    parts = parts_data.get("parts", [])
    if not vehicle or not parts:
        logger.info(
            "Parts audit skipped for %s (%s): mod returned no %s — player "
            "may have no spawned vehicle yet",
            player_name, character_guid,
            "vehicle" if not vehicle else "parts data",
        )
        return None

    embed = _audit_embed(player_name, vehicle, parts, source)

    channel, disabled = await _resolve_audit_channel(discord_client)
    if channel is None:
        if disabled:
            # Feature wired but channel unset/off: log the summary so it's
            # still observable in the worker journal.
            logger.info("Parts audit (%s) %s: %s", source, player_name, " | ".join(summarize_parts(parts)))
            return embed
        # Configured but unresolvable: private channel the bot can't see,
        # deleted channel, stale ID. Loud — this must not pass silently.
        logger.warning(
            "Parts audit channel %s not found (bot lacks View Channel access "
            "or channel is gone) — report for %s NOT delivered",
            settings.DISCORD_PARTS_LOG_CHANNEL_ID, player_name,
        )
        return None
    try:
        await channel.send(embed=embed)
    except Exception:
        logger.warning("Parts audit Discord post failed for %s", player_name, exc_info=True)
        return None
    logger.info("Parts audit delivered for %s (source: %s)", player_name, source)
    return embed


async def audit_event_join(
    http_client_mod, character_guid: str, player_name: str, event_name: str, discord_client=None
) -> discord.Embed | None:
    """Event-join wrapper: audit one newly joined participant."""
    return await audit_character(
        http_client_mod,
        character_guid,
        player_name,
        discord_client=discord_client,
        source=f"event-join: {event_name}",
    )
