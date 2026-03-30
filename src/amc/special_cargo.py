"""Special cargo handler registry.

Certain cargo keys (e.g. "Money") trigger custom side effects beyond the
standard delivery/subsidy flow. This module provides a registry mapping
cargo keys to async handler functions and a single dispatch entry point
called from handle_cargo_arrived() in webhook.py.
"""

import asyncio
import logging

from django.db.models import F
from collections import defaultdict
from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Any

from django.core.cache import cache
from django.utils import timezone

from amc.game_server import announce
from amc.models import Confiscation, CriminalRecord, ServerCargoArrivedLog
from amc.player_tags import refresh_player_name
from amc_finance.services import record_treasury_expense

logger = logging.getLogger("amc.special_cargo")

CRIMINAL_LEVEL_STEP = 50_000


def calculate_criminal_level(laundered_total: int) -> int:
    """Calculate criminal level from cumulative laundered amount.
    Level scales infinitely: floor(total / step) + 1"""
    return (laundered_total // CRIMINAL_LEVEL_STEP) + 1

# Handler signature: (logs, character, http_client, http_client_mod) -> None
SpecialCargoHandler = Callable[
    [list[ServerCargoArrivedLog], Any, Any, Any],
    Coroutine[Any, Any, None],
]


async def _announce_laundered_after_delay(character_guid, http_client, delay=15):
    """Wait for the debounce window, then announce the accumulated total."""
    await asyncio.sleep(delay)
    cache_key = f"money_laundered:{character_guid}"
    data = await cache.aget(cache_key)
    await cache.adelete(cache_key)
    if data and data.get("total", 0) > 0:
        total = data["total"]
        name = data.get("name", "Unknown")
        await announce(
            f"${total:,} has been laundered by {name}",
            http_client,
            color="FFA500",
        )


async def _announce_money_secured_after_delay(character_guid, version, http_client, delay):
    """Wait for the confiscation window, then announce if money wasn't confiscated."""
    await asyncio.sleep(delay)
    cache_key = f"money_secured:{character_guid}"
    data = await cache.aget(cache_key)
    if not data or data.get("version") != version:
        return  # superseded by a newer delivery
    await cache.adelete(cache_key)

    # Check if any confiscation happened during the window
    from amc.commands.faction import ARREST_CONFISCATION_WINDOW

    window_start = timezone.now() - timedelta(minutes=ARREST_CONFISCATION_WINDOW)
    was_confiscated = await Confiscation.objects.filter(
        character__guid=character_guid,
        created_at__gte=window_start,
    ).aexists()
    if was_confiscated:
        return

    total = data.get("total", 0)
    name = data.get("name", "Unknown")
    if total > 0:
        await announce(
            f"{name}'s ${total:,} is now safe from police",
            http_client,
            color="43B581",
        )


async def handle_money_cargo(
    logs: list[ServerCargoArrivedLog],
    character,
    http_client,
    http_client_mod,
) -> None:
    """Side effects for Money deliveries.

    - Create or reset criminal record (7 days from now)
    - Refresh player name tag ([C])
    - Debounced laundering announcement (15s window)
    - Record 20% treasury cost
    """
    # --- Accumulate laundered total for criminal level ---
    money_payment = sum(log.payment for log in logs)
    if money_payment > 0:
        character.criminal_laundered_total = F("criminal_laundered_total") + money_payment
        await character.asave(update_fields=["criminal_laundered_total"])
        await character.arefresh_from_db(fields=["criminal_laundered_total"])

    # --- Criminal record ---
    active_record = await (
        CriminalRecord.objects.filter(
            character=character, expires_at__gt=timezone.now()
        ).afirst()
    )
    if active_record:
        active_record.expires_at = timezone.now() + timedelta(days=7)
        await active_record.asave(update_fields=["expires_at"])
    else:
        await CriminalRecord.objects.acreate(
            character=character,
            reason="Money delivery",
            expires_at=timezone.now() + timedelta(days=7),
        )

    # --- Player tag refresh ---
    await refresh_player_name(character, http_client_mod)

    # --- Debounced announcement + treasury cost ---
    if money_payment > 0:
        if http_client:
            cache_key = f"money_laundered:{character.guid}"
            prev_data = await cache.aget(cache_key)
            if not prev_data:
                await cache.aset(
                    cache_key,
                    {"total": money_payment, "name": character.name},
                    timeout=60,
                )
                asyncio.create_task(
                    _announce_laundered_after_delay(
                        character.guid, http_client, delay=15
                    )
                )
            else:
                prev_data["total"] = prev_data.get("total", 0) + money_payment
                await cache.aset(cache_key, prev_data, timeout=60)
        laundering_cost = int(money_payment * 0.20)
        if laundering_cost > 0:
            await record_treasury_expense(laundering_cost, "Money Laundering Cost")

    # --- Debounced "money secured" announcement after confiscation window ---
    if money_payment > 0 and http_client:
        from amc.commands.faction import ARREST_CONFISCATION_WINDOW

        secured_cache_key = f"money_secured:{character.guid}"
        prev_secured = await cache.aget(secured_cache_key)
        version = (prev_secured.get("version", 0) + 1) if prev_secured else 1
        secured_total = (
            (prev_secured.get("total", 0) + money_payment)
            if prev_secured
            else money_payment
        )
        secured_data = {
            "total": secured_total,
            "name": character.name,
            "version": version,
        }
        await cache.aset(
            secured_cache_key,
            secured_data,
            timeout=ARREST_CONFISCATION_WINDOW * 60 + 120,
        )
        asyncio.create_task(
            _announce_money_secured_after_delay(
                character.guid,
                version,
                http_client,
                delay=ARREST_CONFISCATION_WINDOW * 60,
            )
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SPECIAL_CARGO_HANDLERS: dict[str, SpecialCargoHandler] = {
    "Money": handle_money_cargo,
}


async def run_special_cargo_handlers(
    logs: list[ServerCargoArrivedLog],
    character,
    http_client,
    http_client_mod,
) -> None:
    """Dispatch special-cargo handlers for all cargo keys present in *logs*."""
    if not character:
        return
    logs_by_key: dict[str, list[ServerCargoArrivedLog]] = defaultdict(list)
    for log in logs:
        if log.cargo_key in SPECIAL_CARGO_HANDLERS:
            logs_by_key[log.cargo_key].append(log)
    for key, matching_logs in logs_by_key.items():
        await SPECIAL_CARGO_HANDLERS[key](matching_logs, character, http_client, http_client_mod)

