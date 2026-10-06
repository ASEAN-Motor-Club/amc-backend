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
from datetime import timedelta
from collections import defaultdict
from collections.abc import Callable, Coroutine
from typing import Any

from django.core.cache import cache
from django.utils import timezone

from amc.game_server import announce
from amc.mod_server import show_popup, transfer_money
from amc.models import Character, Delivery, ServerCargoArrivedLog
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
    "CocaineBase",
    "CocaineBricks",
    "CocainePackets",
    "CocaineBags",
    "LiquidCocaine",
    "MoonshineBottles",
    "MethBase",
    "CrystalMeth",
    "HiddenMethBed",
    "HiddenMethSofa",
    "HiddenMethArmchair",
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
# Half-score 300k (freeman 2026-10-03 "let's go"): calibrated against the
# observed criminal-score population on prod — months of play top out at
# ~56k, so the old 2.5M half-score left EVERY real criminal at the 100k
# floor and the score term was effectively dead. At 300k, a 56k kingpin's
# yardstick is ~249k: an 88k haul rolls ~28% for them vs ~68% fresh.
WANTED_YARDSTICK_HALF_SCORE = 300_000
WANTED_COP_ATTENUATION_METRES = 1000.0  # ramp length to the nearest effective cop
WANTED_COP_ATTENUATION_EXPONENT = 2.0  # ramp shape: sweep scales with (d/range)^γ

# Recency-history multiplier (freeman 2026-10-03): the trigger chance also
# depends on the player's RECENT delivery pattern, not just pay-vs-score.
# Multiple large illegal deliveries in a short window make the roll MORE
# likely; interleaved legal deliveries make it LESS likely. Computed from
# the Delivery history at roll time (no new schema).
WANTED_SPREE_WINDOW = timedelta(hours=24)  # spree lookback window
WANTED_SPREE_STEP = 0.25  # chance multiplier added per spree-counting delivery
WANTED_SPREE_CAP = 4  # deliveries counted; cap ⇒ ×2.0 max spree factor
WANTED_CLEAN_STEP = 0.10  # chance multiplier removed per clean credit
WANTED_CLEAN_CREDIT_CAP = 5.0  # max credits ⇒ ×0.5 min clean factor
WANTED_CLEAN_MIN_PAY = 20_000  # a legal delivery pays at least this to count
WANTED_CLEAN_CREDIT_SCALE = 50_000  # credit = min(1, pay / scale)
# Legal deliveries that are meth-chain precursors never count as "clean"
# cover — washing heat by running the very inputs of the next cook defeats
# the mechanic. Values observed paying >250k in real history.
WANTED_CLEAN_EXCLUDED_KEYS = {
    "Acetone",
    "CausticSoda",
    "Fuel",
    "HydrochloricAcid",
    "Quicklime",
    "QuicklimePallet",
    "SulfuricAcid",
}
WANTED_HISTORY_CLAMP = (0.5, 2.0)  # combined multiplier bounds
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


def clamp_history_multiplier(mult: float) -> float:
    lo, hi = WANTED_HISTORY_CLAMP
    return max(lo, min(hi, mult))


async def history_wanted_multiplier(character, before_ts=None) -> float:
    """Recency-history chance multiplier from the player's Delivery history.

    Spree factor: each illicit delivery inside the last
    WANTED_SPREE_WINDOW (strictly before *before_ts*, so the delivery being
    rolled never counts itself) adds WANTED_SPREE_STEP, capped at
    WANTED_SPREE_CAP. Clean factor: legal deliveries (non-illicit, not a
    meth-chain precursor, paying ≥ WANTED_CLEAN_MIN_PAY) inside the SAME
    window add a credit of min(1, pay/WANTED_CLEAN_CREDIT_SCALE) each,
    capped at WANTED_CLEAN_CREDIT_CAP credits; each credit removes
    WANTED_CLEAN_STEP. Using one window for both is deliberate: legal
    runs INTERLEAVED between illicit ones buy partial relief (they would
    count for nothing under a "since the last illicit" rule). Product
    clamped to WANTED_HISTORY_CLAMP so a clean stretch can never halve the
    floor sweep below ×0.5 and a spree can never more than double it.

    *before_ts* defaults to now; pass the delivery timestamp so the batch
    being rolled (same-arrival rows) is excluded from its own history.
    """
    before = before_ts or timezone.now()
    since = before - WANTED_SPREE_WINDOW
    spree_count = await Delivery.objects.filter(
        character=character,
        cargo_key__in=ILLICIT_CARGO_KEYS,
        timestamp__gte=since,
        timestamp__lt=before,
    ).acount()
    spree_factor = 1.0 + WANTED_SPREE_STEP * min(WANTED_SPREE_CAP, spree_count)

    credits = 0.0
    clean_qs = (
        Delivery.objects.filter(
            character=character,
            timestamp__lt=before,
            timestamp__gte=since,
            payment__gte=WANTED_CLEAN_MIN_PAY,
        )
        .exclude(cargo_key__in=ILLICIT_CARGO_KEYS)
        .exclude(cargo_key__in=WANTED_CLEAN_EXCLUDED_KEYS)
    )
    async for row in clean_qs:
        credits += min(1.0, row.payment / WANTED_CLEAN_CREDIT_SCALE)
    clean_factor = 1.0 - WANTED_CLEAN_STEP * min(WANTED_CLEAN_CREDIT_CAP, credits)
    return clamp_history_multiplier(spree_factor * clean_factor)


def wanted_trigger_chance(
    pay: int,
    score: int,
    cop_distance_m: float | None,
    history_multiplier: float = 1.0,
) -> float:
    """Chance (0..1) that one illicit delivery creates a Wanted record.

    Guarantee + ratio-driven (freeman 2026-09-23): a delivery of
    WANTED_GUARANTEE_PAY or more is wanted outright at its UNattenuated chance
    (1.0; attenuated 2026-09-27 per freeman — the cop-proximity multiplier now
    scales the guarantee down toward the floor like any other chance, so a 1M
    haul under a point-blank cop rolls only the floor). Below that, *pay* is
    measured against the criminal's yardstick — their lifetime illicit total
    *score* (measured before this delivery), which saturates at
    WANTED_YARDSTICK_ASYMPTOTE so mid-size hauls plateau at very high scores
    while large hauls stay dangerous. The ratio sweep saturates between the
    floor and ceiling chances through a quadratic knee; the cop-proximity
    attenuation then scales everything above the floor by distance to the
    nearest effective cop, so camping a delivery site farms nothing.
    ``history_multiplier`` (freeman 2026-10-03) scales the sweep above the
    floor by the player's recent delivery pattern — multiple large illegal
    deliveries in 24h raise it, interleaved legal deliveries lower it
    (see history_wanted_multiplier). The guarantee path and the marked
    path are NOT history-scaled — the guarantee is a pay fact, not a
    behavioral one, and the mark is a deliberate escalation.
    """
    if pay >= WANTED_GUARANTEE_PAY:
        return WANTED_TRIGGER_FLOOR_CHANCE + cop_attenuation_multiplier(
            cop_distance_m
        ) * (1.0 - WANTED_TRIGGER_FLOOR_CHANCE)
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
    return min(
        WANTED_TRIGGER_CEILING_CHANCE,
        WANTED_TRIGGER_FLOOR_CHANCE
        + cop_attenuation_multiplier(cop_distance_m)
        * clamp_history_multiplier(history_multiplier)
        * (base - WANTED_TRIGGER_FLOOR_CHANCE),
    )


def should_trigger_wanted(
    pay: int,
    score: int,
    cop_distance_m: float | None,
    marked: bool = False,
    history_multiplier: float = 1.0,
) -> bool:
    """Roll whether this illicit delivery triggers a Wanted level.

    *pay* is the accumulated delivery total within the current debounce
    window, so splitting deliveries (e.g. one cargo at a time) is equivalent
    to a single large delivery. *score* is the criminal's lifetime illicit
    total measured BEFORE this delivery accrues. *cop_distance_m* is the
    distance in metres to the nearest effective cop (None = unknown →
    unattenuated). Callers must not roll at all when there is NO effective
    cop — the wanted system is dormant then (see amc.criminals). Deliveries
    of WANTED_GUARANTEE_PAY or more bypass the ratio sweep but still roll the
    attenuated chance (cop-proximity applies; 2026-09-27 freeman retune).

    *marked* = the character carries a /markwanted flag: the delivery
    triggers with certainty, but the cop-proximity attenuation still applies
    (the 1km no-police rule) — a cop camping point-blank attenuates the
    guaranteed chance down to the floor, same as the organic sweep.
    """
    if marked:
        chance = WANTED_TRIGGER_FLOOR_CHANCE + cop_attenuation_multiplier(
            cop_distance_m
        ) * (1.0 - WANTED_TRIGGER_FLOOR_CHANCE)
        return random.random() < chance
    if pay >= WANTED_GUARANTEE_PAY:
        chance = WANTED_TRIGGER_FLOOR_CHANCE + cop_attenuation_multiplier(
            cop_distance_m
        ) * (1.0 - WANTED_TRIGGER_FLOOR_CHANCE)
        return random.random() < chance
    return random.random() < wanted_trigger_chance(
        pay, score, cop_distance_m, history_multiplier
    )


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
    "CocaineBase": handle_contraband_cargo,
    "CocaineBricks": handle_contraband_cargo,
    "CocainePackets": handle_contraband_cargo,
    "CocaineBags": handle_contraband_cargo,
    "LiquidCocaine": handle_contraband_cargo,
    "MoonshineBottles": handle_contraband_cargo,
    "MethBase": handle_contraband_cargo,
    "CrystalMeth": handle_contraband_cargo,
    "HiddenMethBed": handle_contraband_cargo,
    "HiddenMethSofa": handle_contraband_cargo,
    "HiddenMethArmchair": handle_contraband_cargo,
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
