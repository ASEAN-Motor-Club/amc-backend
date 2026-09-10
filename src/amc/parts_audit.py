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


# Delivery statuses reported by _post_audit_embed.
_DELIVERED = "delivered"
_CLIENT_NOT_READY = "client-not-ready"
_CHANNEL_UNRESOLVABLE = "channel-unresolvable"


async def _post_audit_embed(client, channel_id: int, embed: discord.Embed) -> str:
    """Deliver the embed on the Discord client's OWN event loop.

    Returns one of _DELIVERED / _CLIENT_NOT_READY / _CHANNEL_UNRESOLVABLE;
    raises on transport failure.  The "disabled" case (channel id 0 / no
    client) never reaches here — audit_character treats it as
    "embed built, delivery intentionally skipped".

    WHY THE BRIDGE: the worker runs the bot on a dedicated thread
    (worker.run_discord → client.run), so the client's HTTP session and
    every awaitable it owns belong to that thread's loop.  Awaiting
    ``channel.send`` directly from the worker (arq) loop makes aiohttp's
    timer check (``current_task(bot_loop)``) fail with "Timeout context
    manager should be used inside a task" — every automatic parts-audit
    post failed this way since #106 shipped (prod 2026-09-10), while
    /check_all_players kept working because cogs run on the bot loop.
    Bridge with ``run_coroutine_threadsafe`` — the same pattern the
    event-embed editor (handlers/events.py) and the forward queue
    (amc/tasks.py) already use.  Bounded: the inner ready-wait is capped
    at 10s, the whole bridged send at 30s, so a dead bot thread delays
    the hook pipeline by at most 30s per audit and never hangs it.
    """
    async def _post() -> str:
        if not client.is_ready():
            try:
                await asyncio.wait_for(client.wait_until_ready(), timeout=10)
            except asyncio.TimeoutError:
                return _CLIENT_NOT_READY
        channel = client.get_channel(channel_id)
        if channel is None:
            # Private channel the bot can't see, deleted channel, stale
            # ID — a misconfiguration the caller must surface.
            return _CHANNEL_UNRESOLVABLE
        await channel.send(embed=embed)
        return _DELIVERED

    try:
        client_loop = client.loop
    except Exception:  # noqa: BLE001 — any client-state failure means "no loop"
        client_loop = None
    if client_loop is None:
        raise RuntimeError("Discord client has no event loop (bot not running)")
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if client_loop is running:
        return await _post()
    return await asyncio.wait_for(
        asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_post(), client_loop)),
        timeout=30,
    )


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

    channel_id = int(settings.DISCORD_PARTS_LOG_CHANNEL_ID or 0)
    if not channel_id or discord_client is None:
        # Feature wired but channel unset/off: log the summary so it's
        # still observable in the worker journal.
        logger.info("Parts audit (%s) %s: %s", source, player_name, " | ".join(summarize_parts(parts)))
        return embed

    try:
        status = await _post_audit_embed(discord_client, channel_id, embed)
    except Exception:
        logger.warning("Parts audit Discord post failed for %s", player_name, exc_info=True)
        return None
    if status == _DELIVERED:
        logger.info("Parts audit delivered for %s (source: %s)", player_name, source)
        return embed
    if status == _CLIENT_NOT_READY:
        logger.warning(
            "Parts audit skipped — Discord client not ready (report for %s NOT delivered)",
            player_name,
        )
        return None
    # _CHANNEL_UNRESOLVABLE: loud — this must not pass silently.
    logger.warning(
        "Parts audit channel %s not found (bot lacks View Channel access "
        "or channel is gone) — report for %s NOT delivered",
        channel_id, player_name,
    )
    return None


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
