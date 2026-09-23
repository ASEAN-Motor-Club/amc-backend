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
from typing import Any

from django.core.cache import cache
from django.utils import timezone

from amc.game_server import announce
from amc.mod_server import show_popup, transfer_money
from amc.models import Character, ServerCargoArrivedLog
from amc_finance.services import record_treasury_expense, register_player_deposit

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

# Wanted trigger chance (freeman design 2026-09-23 — 1M guarantee + ratio
# sweep against a SATURATING yardstick). A delivery of WANTED_GUARANTEE_PAY or
# more is always wanted (no roll, cop attenuation ignored); below that, pay is
# measured against the criminal's yardstick — their lifetime illicit total
# *score* (measured before this delivery), which saturates at
# WANTED_YARDSTICK_ASYMPTOTE so the 300k haul plateaus ~25% at very high
# scores while a 1M haul is guaranteed for everyone. Spec: this PR thread.
WANTED_GUARANTEE_PAY = 1_000_000  # illicit deliveries ≥ this are always wanted
WANTED_TRIGGER_FLOOR_CHANCE = 0.05  # ambient risk on every illicit delivery, all ranks
WANTED_TRIGGER_CEILING_CHANCE = 1.0  # asymptotic ceiling of the ratio sweep
WANTED_TRIGGER_KNEE_RATIO = 0.62  # ratio at the sweep midpoint (P = 52.5%)
WANTED_YARDSTICK_FLOOR = 100_000  # lifetime-total reference for fresh records
WANTED_YARDSTICK_ASYMPTOTE = 1_050_000  # yardstick saturation (300k plateau ~25%)
WANTED_YARDSTICK_HALF_SCORE = 2_500_000  # score at which the yardstick is halfway saturated
WANTED_COP_ATTENUATION_METRES = 1000.0  # ramp length to the nearest effective cop
WANTED_COP_ATTENUATION_EXPONENT = 2.0  # ramp shape: sweep scales with (d/range)^γ
# Minimum bounty placed on a Wanted record (creation or per-delivery increment).
# Bounty starts at 0 and only grows from police proximity (chase) in tick_wanted_countdown.
WANTED_MIN_BOUNTY = 0
# How long (seconds) to accumulate illicit deliveries before resetting the window
ILLICIT_DELIVERY_DEBOUNCE = 30


def calculate_criminal_level(criminal_score: int) -> int:
    """Calculate criminal level from the current criminal score.
    Level scales infinitely: floor(score / step) + 1 — derived live, so it
    decays and drops with the score (same pattern as gov employee level)."""
    return (criminal_score // CRIMINAL_LEVEL_STEP) + 1


def cop_attenuation_multiplier(cop_distance_m: float | None) -> float:
    """Sweep multiplier for the distance to the nearest effective cop.

    1.0 beyond WANTED_COP_ATTENUATION_METRES (no attenuation), →0 point-blank.
    ``None`` (no distance known) fails open to 1.0 — the attenuation is
    anti-abuse, not a safety interlock.
    """
    if cop_distance_m is None:
        return 1.0
    x = cop_distance_m / WANTED_COP_ATTENUATION_METRES
    x = max(0.0, min(1.0, x))
    return x**WANTED_COP_ATTENUATION_EXPONENT


def wanted_trigger_chance(pay: int, score: int, cop_distance_m: float | None) -> float:
    """Chance (0..1) that one illicit delivery creates a Wanted record.

    Guarantee + ratio-driven (freeman 2026-09-23): a delivery of
    WANTED_GUARANTEE_PAY or more is wanted outright (returns 1.0 — the roll
    and the cop-proximity attenuation are bypassed). Below that, *pay* is
    measured against the criminal's yardstick — their lifetime illicit total
    *score* (measured before this delivery), which saturates at
    WANTED_YARDSTICK_ASYMPTOTE so mid-size hauls plateau at very high scores
    while large hauls stay dangerous. The ratio sweep saturates between the
    floor and ceiling chances through a quadratic knee; the cop-proximity
    attenuation then scales everything above the floor by distance to the
    nearest effective cop, so camping a delivery site farms nothing.
    """
    if pay >= WANTED_GUARANTEE_PAY:
        return 1.0
    ref = WANTED_YARDSTICK_FLOOR + (
        (WANTED_YARDSTICK_ASYMPTOTE - WANTED_YARDSTICK_FLOOR)
        * score
        / (score + WANTED_YARDSTICK_HALF_SCORE)
    )
    ratio = pay / ref
    ratio_sq = ratio * ratio
    knee_sq = WANTED_TRIGGER_KNEE_RATIO * WANTED_TRIGGER_KNEE_RATIO
    sweep = ratio_sq / (ratio_sq + knee_sq)
    base = WANTED_TRIGGER_FLOOR_CHANCE + (
        WANTED_TRIGGER_CEILING_CHANCE - WANTED_TRIGGER_FLOOR_CHANCE
    ) * sweep
    return WANTED_TRIGGER_FLOOR_CHANCE + cop_attenuation_multiplier(cop_distance_m) * (
        base - WANTED_TRIGGER_FLOOR_CHANCE
    )


def should_trigger_wanted(pay: int, score: int, cop_distance_m: float | None) -> bool:
    """Roll whether this illicit delivery triggers a Wanted level.

    *pay* is the accumulated delivery total within the current debounce
    window, so splitting deliveries (e.g. one cargo at a time) is equivalent
    to a single large delivery. *score* is the criminal's lifetime illicit
    total measured BEFORE this delivery accrues. *cop_distance_m* is the
    distance in metres to the nearest effective cop (None = unknown →
    unattenuated). Callers must not roll at all when there is NO effective
    cop — the wanted system is dormant then (see amc.criminals). Deliveries
    of WANTED_GUARANTEE_PAY or more bypass the roll and the attenuation.
    """
    if pay >= WANTED_GUARANTEE_PAY:
        return True
    return random.random() < wanted_trigger_chance(pay, score, cop_distance_m)


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


async def announce_illicit_delivery(character_guid, http_client, delay=15):
    """Wait for the debounce window, then announce the delivery total + bounty.

    The announce fires at wanted-trigger time: *total* is the illegal delivery
    amount that triggered the wanted, *bounty* the frozen 10%-of-score bounty
    issued with it (freeman 2026-09-23: announce both; drop the old
    "laundered" wording whose number was unrelated to the bounty).
    """
    await asyncio.sleep(delay)
    cache_key = f"money_laundered:{character_guid}"
    data = await cache.aget(cache_key)
    await cache.adelete(cache_key)
    if data and data.get("total", 0) > 0:
        total = data["total"]
        bounty = data.get("bounty", 0)
        name = data.get("name", "Unknown")
        bounty_part = (
            f" — ${bounty:,} bounty issued" if bounty > 0 else ""
        )
        await announce(
            f"${total:,} in illegal cargo delivered by {name}{bounty_part}",
            http_client,
            color="FFA500",
        )


BOSS_CUT_FLOOR = 0.05
BOSS_CUT_CAP = 0.20
BOSS_CUT_CURVE_WEIGHT = 0.15


def calculate_boss_cut_ratio(level: int, boss_level: int) -> float:
    """Non-linear boss cut, hard-capped to [BOSS_CUT_FLOOR, BOSS_CUT_CAP].

    Inverted (freeman 2026-09-22): the closer a criminal's level is to the
    boss's, the smaller the cut — close rivals pay the floor, the lowest-level
    criminals pay up to the cap.
    cut_ratio = clamp(0.05 + 0.15 * (1 - level / boss_level)^2, 0.05, 0.20)
    """
    if boss_level <= 0:
        return 0.0
    ratio = min(1.0, max(0.0, level / boss_level))
    raw = BOSS_CUT_FLOOR + BOSS_CUT_CURVE_WEIGHT * (1.0 - ratio) ** 2
    return max(BOSS_CUT_FLOOR, min(BOSS_CUT_CAP, raw))


async def collect_boss_tax(character, payment: int, http_client_mod) -> None:
    """Route the boss cut for an illicit delivery payment.

    freeman ruling (2026-09-20): the cut ALWAYS goes via bank accounts — it is
    deposited into the highest-ranked criminal's Checking Account (amc_finance
    BANK ledger) via register_player_deposit, never a wallet transfer.  The
    payer side is the wallet (the delivery payment just landed there).  The
    deposit is a pure ledger op, so the boss may be offline.
    """
    if payment <= 0:
        return

    boss = (
        await Character.objects.filter(criminal_score__gt=0)
        .select_related("player")
        .order_by("-criminal_score", "pk")
        .afirst()
    )
    if boss is None:
        return
    if boss.pk == character.pk:
        # The top criminal doesn't pay a cut to himself.
        return

    my_level = calculate_criminal_level(character.criminal_score)
    boss_level = calculate_criminal_level(boss.criminal_score)
    ratio = calculate_boss_cut_ratio(my_level, boss_level)
    cut = int(payment * ratio)
    if cut <= 0:
        return

    try:
        await transfer_money(
            http_client_mod,
            -cut,
            "Boss Cut",
            str(character.player_id),
        )
    except Exception:
        logger.warning(
            "collect_boss_tax: wallet deduction failed for %s (cut=$%s) — tax skipped",
            character.name,
            cut,
            exc_info=True,
        )
        return

    try:
        await register_player_deposit(
            cut, boss, boss.player, description=f"Boss Cut from {character.name}"
        )
    except Exception:
        # Money conservation: the wallet leg applied but the ledger leg did
        # not — refund the payer so no value vanishes, then log loudly.
        logger.exception(
            "collect_boss_tax: bank deposit to boss %s failed (cut=$%s from %s)",
            boss.name,
            cut,
            character.name,
        )
        try:
            await transfer_money(
                http_client_mod,
                cut,
                "Boss Cut Refund",
                str(character.player_id),
            )
        except Exception:
            logger.critical(
                "collect_boss_tax: refund ALSO failed — $%s deducted from %s "
                "with no ledger leg; manual audit required",
                cut,
                character.name,
                exc_info=True,
            )
        return

    if http_client_mod and character.guid:
        asyncio.create_task(
            show_popup(
                http_client_mod,
                f"You paid ${cut:,} ({ratio * 100:.0f}%) to boss {boss.name} "
                "— deposited to their bank account.",
                character_guid=character.guid,
            )
        )


async def accumulate_criminal_score(
    character, payment: int, http_client_mod, *, collect_tax: bool = True
) -> None:
    """Add the illicit delivery payment to the character's criminal score.

    The score accumulates the FULL payment — even when the wallet was
    nullified (modded vehicle / invalid delivery), the rap sheet counts the
    delivery. The boss tax is money-side only and is skipped for nullified
    payments (a zeroed profit is not taxed).
    """
    if payment <= 0:
        return
    character.criminal_score = F("criminal_score") + payment
    character.last_illicit_delivery_at = timezone.now()
    await character.asave(
        update_fields=["criminal_score", "last_illicit_delivery_at"]
    )
    await character.arefresh_from_db(fields=["criminal_score"])
    if collect_tax:
        await collect_boss_tax(character, payment, http_client_mod)


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

    - Accumulate criminal_score (rap sheet counts the full payment) + boss cut
    - Debounced laundering announcement (15s window)
    - Record 20% treasury cost
    - Zero out wallet payment if delivered with a modded vehicle
    """
    money_payment = sum(log.payment for log in logs)

    # --- Zero out wallet payment for modded vehicle or invalid delivery (DeliveryId == -1) ---
    delivery_ids = [log.data.get("Net_DeliveryId") for log in logs if log.data]
    is_invalid_delivery = any(did == -1 for did in delivery_ids)
    payment_nullified = False
    if (is_modded or is_invalid_delivery) and money_payment > 0 and http_client_mod:
        payment_nullified = True
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

    # --- Criminal score: the full payment counts toward the rap sheet even
    # when the wallet was nullified; the boss tax only fires when the wallet
    # actually received the money ---
    if money_payment > 0:
        await accumulate_criminal_score(
            character,
            money_payment,
            http_client_mod,
            collect_tax=not payment_nullified,
        )

    # --- Treasury cost ---
    if money_payment > 0:
        laundering_cost = int(money_payment * 0.20)
        if laundering_cost > 0:
            await record_treasury_expense(laundering_cost, "Money Laundering Cost")


async def handle_contraband_cargo(
    logs: list[ServerCargoArrivedLog],
    character,
    http_client,
    http_client_mod,
    is_modded: bool = False,
) -> None:
    """Side effects for contraband deliveries (Ganja, Cocaine, etc.).

    - Accumulate criminal_score (rap sheet counts the full payment) + boss cut
    - Zero out wallet payment if delivered with a modded vehicle
    """
    # --- Zero out wallet payment for modded vehicle or invalid delivery (DeliveryId == -1) ---
    delivery_payment = sum(log.payment for log in logs)
    delivery_ids = [log.data.get("Net_DeliveryId") for log in logs if log.data]
    is_invalid_delivery = any(did == -1 for did in delivery_ids)
    payment_nullified = False
    if (is_modded or is_invalid_delivery) and delivery_payment > 0 and http_client_mod:
        payment_nullified = True
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

    # --- Criminal score: the full payment counts toward the rap sheet even
    # when the wallet was nullified; the boss tax only fires when the wallet
    # actually received the money ---
    if delivery_payment > 0:
        await accumulate_criminal_score(
            character,
            delivery_payment,
            http_client_mod,
            collect_tax=not payment_nullified,
        )


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
