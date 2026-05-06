"""ASEAN Tax engine.

Mirrors the structure of `amc.subsidies.get_subsidy_for_cargo` /
`subsidise_player`, but flows funds INTO the treasury instead of out.

Tax is computed on the *raw* base cargo payment (post-fraud-clawback,
pre-subsidy, pre-supply-chain-bonus). Treasury cap is *inverted* so tax
decreases as the treasury fills, supporting the design goal of keeping
net flow positive into the treasury while protecting players from
runaway taxation when the government is already rich.
"""

from __future__ import annotations

import logging

from django.contrib.gis.geos import Point
from django.db.models import F, Q

from amc.models import TaxRule
from amc.mod_server import transfer_money
from amc_finance.services import record_treasury_tax_collection

logger = logging.getLogger(__name__)


def _is_experienced(character) -> bool:
    """
    `EXPERIENCED_DRIVER_LEVEL_THRESHOLD`. 
    Experienced players bypass tax throttling
    """
    if character is None:
        return False
    from amc import config

    threshold = int(config.EXPERIENCED_DRIVER_LEVEL_THRESHOLD or 0)
    if threshold <= 0:
        return False
    driver_level = int(getattr(character, "driver_level", 0) or 0)
    return driver_level >= threshold


def _experienced_bypass_strength(treasury_balance) -> float:
    """Return experienced-player bypass strength in [0, 1].

    1.0 = full bypass (player ignores throttles entirely).
    0.0 = no bypass (player uses normal throttled tax).

    Below `TREASURY_GOOD_HEALTH_T` the bypass is at full strength. Between
    `TREASURY_GOOD_HEALTH_T` and 1.0 (ceiling) it ramps linearly down to 0
    so a near-ceiling treasury stops over-collecting from veterans.
    """
    if treasury_balance is None:
        # No treasury context — assume worst case (poor) so experienced
        # players pay full.
        return 1.0
    from amc import config

    floor = float(config.TREASURY_FLOOR)
    ceiling = float(config.TREASURY_CEILING)
    if ceiling <= floor:
        return 0.0
    bal = float(treasury_balance)
    t = max(0.0, min(1.0, (bal - floor) / (ceiling - floor)))
    good_t = max(0.0, min(1.0, float(config.TREASURY_GOOD_HEALTH_T)))
    if t <= good_t:
        return 1.0
    if good_t >= 1.0:
        return 1.0
    return max(0.0, 1.0 - (t - good_t) / (1.0 - good_t))


def _compute_tax_scale(treasury_balance) -> float:
    """Inverted treasury scale: poor -> 1.0, at/above ceiling -> 0.0."""
    from amc import config

    floor = float(config.TREASURY_FLOOR)
    ceiling = float(config.TREASURY_CEILING)
    bal = float(treasury_balance)
    if ceiling <= floor or bal >= ceiling:
        return 0.0
    if bal <= floor:
        return 1.0
    t = (bal - floor) / (ceiling - floor)
    exponent = max(0.0001, float(config.TREASURY_CURVE_EXPONENT))
    return (1.0 - t) ** exponent


async def get_tax_for_cargo(cargo, treasury_balance=None, character=None):
    """Find the highest-priority TaxRule that matches this cargo log.

    Returns (tax_amount, tax_factor, best_rule).

    Mirrors `amc.subsidies.get_subsidy_for_cargo` matching logic exactly.
    Treasury cap is inverted: high treasury → tax scales down to 0.
    """

    from amc import config as _cfg

    if not _cfg.WEALTH_TAX_SYSTEM_ENABLED:
        return 0, 0.0, None

    rules = TaxRule.objects.filter(active=True).order_by("-priority")

    # 1. Cargo Key Filter
    rules = rules.filter(Q(cargos__isnull=True) | Q(cargos__key=cargo.cargo_key))

    # 2. Source Filter
    if cargo.sender_point and cargo.sender_point.coord:
        rules = rules.filter(
            Q(source_areas__isnull=True, source_delivery_points__isnull=True)
            | Q(source_areas__polygon__contains=cargo.sender_point.coord)
            | Q(source_delivery_points__coord__dwithin=(cargo.sender_point.coord, 1.0))
        )
    else:
        rules = rules.filter(
            source_areas__isnull=True, source_delivery_points__isnull=True
        )

    # 3. Destination Filter
    destination_coord = None
    if cargo.destination_point and cargo.destination_point.coord:
        destination_coord = cargo.destination_point.coord
    elif destination_location := cargo.data.get("Net_DestinationLocation"):
        destination_coord = Point(
            destination_location["X"],
            destination_location["Y"],
            destination_location["Z"],
            srid=3857,
        )

    if destination_coord:
        rules = rules.filter(
            Q(destination_areas__isnull=True, destination_delivery_points__isnull=True)
            | Q(destination_areas__polygon__contains=destination_coord)
            | Q(destination_delivery_points__coord__dwithin=(destination_coord, 1.0))
        )
    else:
        rules = rules.filter(
            destination_areas__isnull=True, destination_delivery_points__isnull=True
        )

    # 4. On Time Check
    is_on_time = cargo.data.get("Net_TimeLeftSeconds", 0) > 0
    if not is_on_time:
        rules = rules.exclude(requires_on_time=True)

    best_rule = await rules.distinct().afirst()

    tax_factor = 0.0
    tax_amount = 0
    if best_rule:
        factor = float(best_rule.tax_value)
        if best_rule.tax_type == TaxRule.TaxType.PERCENTAGE:
            tax_factor = factor
            tax_amount = int(int(cargo.payment) * tax_factor)
        else:
            tax_amount = int(factor)
            if cargo.payment > 0:
                tax_factor = tax_amount / cargo.payment

    # Unified treasury scale — same FLOOR/CEILING/EXPONENT knobs in
    # `amc.config` that drive ASEAN subsidies and /jobs payouts, just
    # inverted: poor treasury -> full tax, rich treasury -> zero tax.
    #   t = clamp01((balance - FLOOR) / (CEILING - FLOOR))
    #   tax_scale = (1 - t) ** EXPONENT   (mirror of subsidy_scale = t ** EXPONENT)
    # Experienced players blend between full-bypass (tax_scale=1) and
    # normal scale based on `_experienced_bypass_strength`.
    if treasury_balance is not None and tax_amount > 0:
        normal_scale = _compute_tax_scale(treasury_balance)
        if _is_experienced(character):
            bypass = _experienced_bypass_strength(treasury_balance)
            tax_scale = bypass * 1.0 + (1.0 - bypass) * normal_scale
        else:
            tax_scale = normal_scale
        tax_amount = int(tax_amount * tax_scale)
        if cargo.payment > 0:
            tax_factor = tax_amount / cargo.payment

    return tax_amount, tax_factor, best_rule


async def apply_tax_player_cuts(tax_amount, character, treasury_balance=None, wealth_state=None):
    """Apply the established-player wealth curve to a positive tax amount.

    Mirrors `amc.subsidies.apply_subsidy_player_cuts`. NEW players (lifetime
    income still under the newbie cap) pay no tax; established players pay
    between `WEALTH_TAX_FLOOR_PCT` (when broke) and 100% (when at/above
    `WEALTH_RICH_CEILING`), with the curve warped by
    `WEALTH_EXPONENT` so the rich pay disproportionately more.

        t_warp  = wealth_t ** WEALTH_EXPONENT
        tax_pct = TAX_FLOOR_PCT + (1 - TAX_FLOOR_PCT) * t_warp

    The base `tax_amount` already has treasury-driven scaling baked in
    (via `get_tax_for_cargo` against TREASURY_FLOOR/CEILING). This layer
    adds the wealth shape on top so even a healthy treasury still pulls
    *some* tax from a rich player while broke established players pay near
    the minimum floor.

    Experienced players (`driver_level ≥ EXPERIENCED_DRIVER_LEVEL_THRESHOLD`)
    bypass the wealth curve entirely while the treasury is below
    `TREASURY_GOOD_HEALTH_T`. Once the treasury enters good health the
    bypass blends linearly toward the normal wealth-curve cut.

    Caller is responsible for the gov-employee skip (handler does it
    upstream so we don't pay for the bank/lifetime lookup unnecessarily).
    """
    if tax_amount <= 0 or character is None:
        return 0

    from amc import config


    if not config.WEALTH_TAX_SYSTEM_ENABLED:
        return 0

    from amc.subsidies import compute_wealth_state

    # Tax hardcutoff once the treasury is at/above CEILING.
    if treasury_balance is not None:
        ceiling = float(config.TREASURY_CEILING)
        if float(treasury_balance) >= ceiling:
            return 0

    floor_pct = float(config.WEALTH_TAX_FLOOR_PCT)
    is_experienced = _is_experienced(character)

    # Compute the wealth-driven tax percentage that would normally apply.
    if wealth_state is None:
        state = await compute_wealth_state(character)
    else:
        state = wealth_state
    if state is None:
        # Lookup failed — apply minimum tax (config floor) so we still
        # collect *something* for the treasury without overcharging a
        # player whose wealth we can't verify. Subsidy path returns 0
        # in the same situation, so the player is no worse off than
        # the floor.
        normal_pct = floor_pct
        is_established = True  # treat as taxable so experienced bypass still applies
    else:
        is_established, wealth_t = state
        if not is_established and not is_experienced:
            # NEW player, not yet experienced — fully exempt from tax.
            return 0
        if is_established:
            punish_exp = max(0.0001, float(config.WEALTH_EXPONENT))
            t_warp = wealth_t ** punish_exp
            normal_pct = floor_pct + (1.0 - floor_pct) * t_warp
        else:
            # NEW + experienced — grinder who hasn't accumulated wealth.
            # Use floor_pct as the "normal" so the experienced bypass can
            # still pull them up toward 100% while treasury is hurting.
            normal_pct = floor_pct

    if is_experienced:
        bypass = _experienced_bypass_strength(treasury_balance)
        tax_pct = bypass * 1.0 + (1.0 - bypass) * normal_pct
    else:
        tax_pct = normal_pct

    return max(0, int(tax_amount * tax_pct))


async def tax_player(amount, character, session, message="ASEAN Tax"):
    """Deduct `amount` from the player's in-game wallet and credit the
    Treasury Fund via a proper double-entry ledger entry.

    Safe for `amount <= 0` (no-op). Safe to call without ledger context
    (the ledger entry handles its own transaction).
    """
    if amount <= 0 or not character or not session:
        return
    try:
        await transfer_money(
            session,
            int(-amount),
            message,
            str(character.player.unique_id),
        )
    except Exception as e:
        # Player wallet may not have the funds. Don't credit the treasury
        # if we couldn't actually take the money from the player.
        logger.warning(
            "Failed to deduct ASEAN Tax from %s (%s): %s",
            character.name,
            character.player.unique_id,
            e,
        )
        return
    try:
        await record_treasury_tax_collection(amount, character, message)
    except Exception:
        # Treasury credit failed — refund the wallet so the player isn't
        # out money the treasury never received. If the refund itself
        # fails we surface to the error reporter for manual reconciliation.
        logger.exception(
            "ASEAN Tax wallet deducted from %s but treasury credit failed; refunding wallet",
            character.name,
        )
        try:
            await transfer_money(
                session,
                int(amount),
                f"{message} (refund: treasury credit failed)",
                str(character.player.unique_id),
            )
        except Exception as refund_exc:
            logger.exception(
                "ASEAN Tax refund ALSO failed for %s — manual reconciliation required",
                character.name,
            )
            from amc import error_reporter

            error_reporter.report_exception(
                refund_exc,
                subject="tax: wallet debited but neither treasury credited nor refunded",
                context={
                    "character": getattr(character, "name", "?"),
                    "player_unique_id": str(
                        getattr(getattr(character, "player", None), "unique_id", "")
                    ),
                    "amount": int(amount),
                    "message": message,
                },
            )


async def record_tax_rule_collection(rule, amount):
    """Increment a TaxRule's lifetime collected counter."""
    if rule and amount > 0:
        await TaxRule.objects.filter(pk=rule.pk).aupdate(
            collected=F("collected") + amount
        )
