"""Special cargo handler registry.

Certain cargo keys (e.g. "Money", "Ganja") trigger custom side effects beyond
the standard delivery/subsidy flow. This module provides a registry mapping
cargo keys to async handler functions and a single dispatch entry point
called from handle_cargo_arrived() in webhook.py.
"""

import asyncio
import logging
import random

from django.db.models import F
from collections import defaultdict
from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Any

from django.core.cache import cache
from django.utils import timezone

from amc.game_server import announce
from amc.mod_server import show_popup, transfer_money
from amc.models import Confiscation, CriminalRecord, ServerCargoArrivedLog
from amc.player_tags import refresh_player_name
from amc_finance.services import record_treasury_expense

logger = logging.getLogger("amc.special_cargo")

CRIMINAL_LEVEL_STEP = 50_000

# All cargo keys that are considered illicit and trigger a Wanted level
ILLICIT_CARGO_KEYS: set[str] = {
    "Money",
    "Ganja",
    "CocaLeavesPallet",
    "GanjaPallet",
    "Cocaine",
    "MoneyPallet",
    "Moonshine",
    "CocaPaste",
    "CocaineBricks",
}

# Wanted trigger probability constants
WANTED_MIN_CHANCE = 0.10  # 10% floor for small deliveries
WANTED_FULL_CHANCE_AMOUNT = 100_000  # $100k+ = 100% chance
# Minimum bounty placed on a Wanted record (creation or per-delivery increment).
# Bounty starts at 0 and only grows from police proximity (chase) in tick_wanted_countdown.
WANTED_MIN_BOUNTY = 0
# How long (seconds) to accumulate illicit deliveries before resetting the window
ILLICIT_DELIVERY_DEBOUNCE = 30


def calculate_criminal_level(laundered_total: int) -> int:
    """Calculate criminal level from cumulative laundered amount.
    Level scales infinitely: floor(total / step) + 1"""
    return (laundered_total // CRIMINAL_LEVEL_STEP) + 1


def should_trigger_wanted(accumulated_amount: int) -> bool:
    """Determine whether illicit deliveries should trigger a Wanted level.

    Uses the *accumulated* delivery total within the current debounce window
    so that splitting deliveries (e.g. one cargo at a time) is equivalent to
    a single large delivery.

    Probability scales linearly with accumulated_amount:
    - Any amount: 10% floor
    - $50k: 50% chance
    - $100k+: 100% chance
    """
    chance = max(WANTED_MIN_CHANCE, min(1.0, accumulated_amount / WANTED_FULL_CHANCE_AMOUNT))
    return random.random() < chance


async def accumulate_illicit_delivery(character_guid: str, amount: int) -> int:
    """Add *amount* to the rolling debounce window total and return the new total.

    The window resets after ILLICIT_DELIVERY_DEBOUNCE seconds of inactivity,
    preventing micro-deliveries (e.g. one cargo per ~5 s) from being evaluated
    individually rather than as an aggregate.
    """
    cache_key = f"illicit_delivery_total:{character_guid}"
    prev = await cache.aget(cache_key, 0)
    new_total = (prev or 0) + amount
    await cache.aset(cache_key, new_total, timeout=ILLICIT_DELIVERY_DEBOUNCE)
    return new_total


# Handler signature: (logs, character, http_client, http_client_mod, is_modded) -> None
SpecialCargoHandler = Callable[
    [list[ServerCargoArrivedLog], Any, Any, Any, bool],
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


async def announce_money_secured(character_guid: str, http_client) -> None:
    """Announce that laundered money is safe, if applicable.

    Called from tick_wanted_countdown when a wanted status expires.
    Checks the money_secured cache (accumulated by handle_money_cargo)
    and announces if no confiscation happened.
    """
    cache_key = f"money_secured:{character_guid}"
    data = await cache.aget(cache_key)
    if not data:
        logger.debug("announce_money_secured(%s): no cache data", character_guid)
        return
    await cache.adelete(cache_key)

    # Check if any confiscation happened during the wanted period
    # Use a generous window since wanted is now permanent until cleared
    window_start = timezone.now() - timedelta(days=7)
    was_confiscated = await Confiscation.objects.filter(
        character__guid=character_guid,
        created_at__gte=window_start,
    ).aexists()
    if was_confiscated:
        logger.info(
            "announce_money_secured(%s): suppressed — confiscation found",
            character_guid,
        )
        return

    total = data.get("total", 0)
    name = data.get("name", "Unknown")
    if total > 0 and http_client:
        logger.info(
            "announce_money_secured(%s): announcing $%s safe for %s",
            character_guid,
            total,
            name,
        )
        await announce(
            f"{name}'s ${total:,} is now safe from police",
            http_client,
            color="43B581",
        )
    else:
        logger.debug(
            "announce_money_secured(%s): skipped — total=%s http_client=%s",
            character_guid,
            total,
            bool(http_client),
        )


async def ensure_criminal_record(
    character, reason: str, http_client_mod=None
) -> CriminalRecord:
    """Ensure an active CriminalRecord exists for the character.

    Active = cleared_at IS NULL. UniqueConstraint ensures at most one.
    Returns the existing active record or creates a new one.
    Triggers a player tag refresh when a NEW record is created (adds [C] tag).
    Called on both cargo load and cargo delivery.
    """
    active_record = await CriminalRecord.objects.filter(
        character=character, cleared_at__isnull=True
    ).afirst()
    if active_record:
        return active_record
    record = await CriminalRecord.objects.acreate(
        character=character,
        reason=reason,
        cleared_at=None,  # NULL = active
    )
    # New record created — refresh tag to show [C] indicator
    asyncio.create_task(refresh_player_name(character, http_client_mod))
    return record


CONFISCATABLE_FRACTION = 0.8  # 80% of delivery payment is at risk of confiscation


async def accumulate_criminal_record_amount(character, payment: int) -> None:
    """Add delivery payment to BOTH amount and confiscatable_amount.

    amount = permanent audit total (never decays).
    confiscatable_amount = decaying total (cron reduces when online).
    Only 80% of the payment is added to confiscatable_amount — the remaining
    20% is guaranteed safe from confiscation.
    """
    confiscatable_portion = int(payment * CONFISCATABLE_FRACTION)
    await CriminalRecord.objects.filter(
        character=character, cleared_at__isnull=True
    ).aupdate(
        amount=F("amount") + payment,
        confiscatable_amount=F("confiscatable_amount") + confiscatable_portion,
    )


async def link_delivery_to_criminal_record(character, cargo_key, timestamp) -> None:
    """Associate the Delivery record created for this cargo with the active CriminalRecord."""
    from amc.models import Delivery

    active_record = await CriminalRecord.objects.filter(
        character=character, cleared_at__isnull=True
    ).afirst()
    if not active_record:
        return
    delivery = await Delivery.objects.filter(
        character=character, cargo_key=cargo_key, timestamp=timestamp
    ).afirst()
    if delivery and not delivery.criminal_record_id:
        delivery.criminal_record = active_record
        await delivery.asave(update_fields=["criminal_record"])


# ---------------------------------------------------------------------------
# Per-cargo-type handlers
# ---------------------------------------------------------------------------


async def handle_money_cargo(
    logs: list[ServerCargoArrivedLog],
    character,
    http_client,
    http_client_mod,
    is_modded: bool = False,
) -> None:
    """Side effects for Money deliveries.

    - Create or ensure active criminal record (refreshes [C] tag on first delivery)
    - Debounced laundering announcement (15s window)
    - Record 20% treasury cost
    - Zero out wallet payment if delivered with a modded vehicle
    """
    # --- Accumulate laundered total for criminal level ---
    money_payment = sum(log.payment for log in logs)
    if money_payment > 0:
        character.criminal_laundered_total = (
            F("criminal_laundered_total") + money_payment
        )
        await character.asave(update_fields=["criminal_laundered_total"])
        await character.arefresh_from_db(fields=["criminal_laundered_total"])

    # --- Zero out wallet payment for modded vehicle or invalid delivery (DeliveryId == -1) ---
    delivery_ids = [log.data.get("Net_DeliveryId") for log in logs if log.data]
    is_invalid_delivery = any(did == -1 for did in delivery_ids)
    if (is_modded or is_invalid_delivery) and money_payment > 0 and http_client_mod:
        message = "Invalid Delivery" if is_invalid_delivery else "Modded Vehicle Confiscation"
        await transfer_money(
            http_client_mod,
            int(-money_payment),
            message,
            str(character.player.unique_id),
        )
        if is_invalid_delivery:
            asyncio.create_task(
                show_popup(
                    http_client_mod,
                    "Your illicit delivery payment was nullified. "
                    "The entire delivery — from pickup to destination — must be completed "
                    "while fully connected to the server.",
                    character_guid=character.guid,
                    player_id=str(character.player.unique_id),
                )
            )

    # --- Criminal record (refresh tag if newly created) ---
    await ensure_criminal_record(character, reason="Money delivery", http_client_mod=http_client_mod)
    if money_payment > 0:
        await accumulate_criminal_record_amount(character, money_payment)

    # --- Refresh name to show updated criminal level ---
    if money_payment > 0 and http_client_mod:
        await refresh_player_name(character, http_client_mod)

    # --- Treasury cost ---
    if money_payment > 0:
        laundering_cost = int(money_payment * 0.20)
        if laundering_cost > 0:
            await record_treasury_expense(laundering_cost, "Money Laundering Cost")

    # --- Accumulate "money secured" total (announced when wanted expires) ---
    if money_payment > 0:
        secured_cache_key = f"money_secured:{character.guid}"
        prev_secured = await cache.aget(secured_cache_key)
        secured_total = (
            (prev_secured.get("total", 0) + money_payment)
            if prev_secured
            else money_payment
        )
        secured_data = {
            "total": secured_total,
            "name": character.name,
        }
        # Use a long timeout since wanted is now permanent until cleared
        await cache.aset(
            secured_cache_key,
            secured_data,
            timeout=7 * 24 * 3600,  # 7 days
        )


async def handle_contraband_cargo(
    logs: list[ServerCargoArrivedLog],
    character,
    http_client,
    http_client_mod,
    is_modded: bool = False,
) -> None:
    """Side effects for contraband deliveries (Ganja, Cocaine, etc.).

    - Create or ensure active criminal record (refreshes [C] tag on first delivery)
    - Accumulate confiscatable amount
    - Increment criminal_laundered_total for criminal level progression
    - Zero out wallet payment if delivered with a modded vehicle
    """
    # --- Accumulate laundered total for criminal level ---
    delivery_payment = sum(log.payment for log in logs)
    if delivery_payment > 0:
        character.criminal_laundered_total = (
            F("criminal_laundered_total") + delivery_payment
        )
        await character.asave(update_fields=["criminal_laundered_total"])
        await character.arefresh_from_db(fields=["criminal_laundered_total"])

    # --- Zero out wallet payment for modded vehicle or invalid delivery (DeliveryId == -1) ---
    delivery_ids = [log.data.get("Net_DeliveryId") for log in logs if log.data]
    is_invalid_delivery = any(did == -1 for did in delivery_ids)
    if (is_modded or is_invalid_delivery) and delivery_payment > 0 and http_client_mod:
        message = "Invalid Delivery" if is_invalid_delivery else "Modded Vehicle Confiscation"
        await transfer_money(
            http_client_mod,
            int(-delivery_payment),
            message,
            str(character.player.unique_id),
        )
        if is_invalid_delivery:
            asyncio.create_task(
                show_popup(
                    http_client_mod,
                    "Your illicit delivery payment was nullified. "
                    "The entire delivery — from pickup to destination — must be completed "
                    "while fully connected to the server.",
                    character_guid=character.guid,
                    player_id=str(character.player.unique_id),
                )
            )

    # --- Criminal record (refresh tag if newly created) ---
    cargo_key = logs[0].cargo_key if logs else "Contraband"
    await ensure_criminal_record(
        character, reason=f"{cargo_key} delivery", http_client_mod=http_client_mod
    )
    if delivery_payment > 0:
        await accumulate_criminal_record_amount(character, delivery_payment)

    # --- Refresh name to show updated criminal level ---
    if delivery_payment > 0 and http_client_mod:
        await refresh_player_name(character, http_client_mod)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SPECIAL_CARGO_HANDLERS: dict[str, SpecialCargoHandler] = {
    "Money": handle_money_cargo,
    "Ganja": handle_contraband_cargo,
    "CocaLeavesPallet": handle_contraband_cargo,
    "GanjaPallet": handle_contraband_cargo,
    "Cocaine": handle_contraband_cargo,
    "MoneyPallet": handle_contraband_cargo,
    "Moonshine": handle_contraband_cargo,
    "CocaPaste": handle_contraband_cargo,
    "CocaineBricks": handle_contraband_cargo,
}


async def run_special_cargo_handlers(
    logs: list[ServerCargoArrivedLog],
    character,
    http_client,
    http_client_mod,
    is_modded: bool = False,
) -> None:
    """Dispatch special-cargo handlers for all cargo keys present in *logs*."""
    if not character:
        return
    logs_by_key: dict[str, list[ServerCargoArrivedLog]] = defaultdict(list)
    for log in logs:
        if log.cargo_key in SPECIAL_CARGO_HANDLERS:
            logs_by_key[log.cargo_key].append(log)
    for key, matching_logs in logs_by_key.items():
        await SPECIAL_CARGO_HANDLERS[key](
            matching_logs, character, http_client, http_client_mod, is_modded
        )
