"""Real-time fraud detection for inflated cargo and passenger payments.

Detects players using mods/cheats that multiply work payouts beyond
base game values.  Two complementary checks per cargo delivery:

1. **Per-unit check**: payment / quantity against per-unit baseline.
   Catches inflated payments regardless of delivery distance.
2. **Distance-per-km check**: payment / distance_km against per-km
   baseline.  Catches inflated payments even when per-unit looks
   normal (e.g. short-distance deliveries with huge payouts).

The maximum clawback from both checks is used.  For passengers, an
absolute per-trip ceiling is enforced per passenger type.

Thresholds are derived from historical data of legitimate deliveries
(mean + 3σ on payment-per-km, excluding known outliers).
"""

import logging
import math
from dataclasses import dataclass

from django.contrib.gis.geos import Point

from amc.models import DeliveryPoint

logger = logging.getLogger("amc.fraud_detection")


# ---------------------------------------------------------------------------
# Threshold tables
#
# Derived from production data as of 2026-04.
# _PER_KM:  upper bound on payment / distance_km for each cargo type.
#           Based on ~p99 of legitimate deliveries (excludes known cheats).
# _PER_UNIT: upper bound on payment / quantity for each cargo type.
#            Based on mean + 3σ of legitimate deliveries (excludes >500k).
# _MAX_ABS:  hard ceiling on total payment per delivery event.
# ---------------------------------------------------------------------------

CARGO_PER_KM_THRESHOLDS: dict[str, float] = {
    "BottlePallete": 200,
    "IronOre": 400,
    "Log_Oak_12ft": 800,
    "GiftBox_01": 300,
    "Concrete": 100,
    "CheeseBox": 350,
    "CheesePallet": 80,
    "CornPallet": 70,
    "Container_20ft_01": 110,
    "Fuel": 250,
    "Coal": 100,
    "Container_40ft_01": 50,
    "MeatBox": 300,
    "ToyBoxes": 250,
    "Log_20ft": 350,
    "WoodPlank_14ft_5t": 100,
    "SteelCoil_10t": 100,
    "Limestone": 100,
    "SunflowerSeed": 100,
    "PlasticPipes_6m": 100,
    "CabbagePallet": 200,
    "BeanPallet": 200,
    "BreadBox": 400,
    "MilitarySupplyBox_01": 800,
    "Moonshine": 1000,
}

# Per-cargo per-UNIT payment ceilings (recalibrated 2026-09-10, PR #117).
#
# Calibration basis: RAW pre-clawback Net_Payment from 90d of prod data,
# threshold ~= max(2 x p99, 1.2 x observed max) rounded up to 5k. The old
# table was fitted on the POST-clawback payment column, which pinned most
# thresholds at exactly the clawed maximum (zero headroom) and clawed
# ~$61M/90d from legitimate heavy hauls (multi-character routes paying
# 60-255k/unit on SteelCoil, containers, PlasticPipes, Moonshine...).
#
# Cheat clusters are deliberately NOT accommodated: single-day spikes
# (Jun 19 / Jul 10-18 / Aug 4 2026) where one character pays 6-30x the
# multi-character route consensus (Coal 63.7k on a 9.5k route, Fuel
# 92-125k on an 8k route, SteelCoil 66-434k on a 23.5k route). Those
# belong to moderation, not threshold headroom — the recalibrated
# ceilings catch them.
CARGO_PER_UNIT_THRESHOLDS: dict[str, float] = {
    "Acetone": 10_000,
    "BeanPallet": 20_000,
    "Bed_01": 15_000,
    "Bed_02": 15_000,
    "Bed_03": 10_000,
    "BottlePallete": 20_000,
    "BreadBox": 12_000,
    "BreadPallet": 10_000,
    "CabbagePallet": 25_000,
    "CheeseBox": 10_000,
    "CheesePallet": 18_000,
    "Coal": 20_000,
    "CocaPaste": 35_000,
    "Concrete": 25_000,
    "Container_20ft_01": 60_000,
    "Container_40ft_01": 90_000,
    "CopperConcentrate": 12_000,
    "CopperOre": 5_000,
    "CopperRodCoil_2t": 100_000,
    "CornPallet": 35_000,
    "CrudeOil": 15_000,
    "Fuel": 20_000,
    "GiftBox_01": 15_000,
    "HempPallet": 35_000,
    "HydrochloricAcid": 12_000,
    "IronOre": 15_000,
    "lHBeam_6m": 100_000,
    "Limestone": 12_000,
    "LiveFish_01": 5_000,
    "Log_20ft": 16_000,
    "Log_Oak_12ft": 15_000,
    "MeatBox": 20_000,
    "MilitarySupplyBox_01": 15_000,
    # "Money" deliberately UNLISTED: it is the laundering/criminal-level
    # cargo whose design anchors (50k/110k laundering totals) sit far above
    # its observed payment median — a per-unit ceiling here would claw
    # legitimate laundering deliveries. It stays covered by
    # CARGO_PER_UNIT_DEFAULT below.
    "MoneyPallet": 80_000,
    "Moonshine": 55_000,
    "Oil": 5_000,
    "OrangeBoxes": 50_000,
    "PlasticPipes_6m": 35_000,
    "PowerBox": 25_000,
    "PumpkinBox": 8_000,
    "PumpkinPallet": 25_000,
    "QuicklimePallet": 6_000,
    "Rice": 5_000,
    "RicePallet": 30_000,
    "Sand": 10_000,
    "FineSand": 8_000,
    "Sofa_01": 10_000,
    "Sofa_02": 8_000,
    "Sofa_03": 12_000,
    "Sofa_04": 8_000,
    "SteelCoil_10t": 35_000,
    "SunflowerSeed": 3_000,
    "TrashBag": 5_000,
    "Trash_Big": 4_000,
    "ToyBoxes": 45_000,
    "WoodPlank_14ft_5t": 15_000,
}

# Backstop for cargo keys not listed above (new game-update cargo is
# otherwise unprotected): any per-unit payment beyond this is excess.
CARGO_PER_UNIT_DEFAULT = 250_000

# Absolute per-delivery ceiling — catches anything absurd regardless of distance.
# Set to ~50x the highest legitimate per-unit avg across all cargo types.
CARGO_MAX_ABSOLUTE_PAYMENT: dict[str, float] = {
    "BottlePallete": 500_000,
    "IronOre": 500_000,
    "Log_Oak_12ft": 500_000,
    "GiftBox_01": 500_000,
    "Concrete": 300_000,
    "CheeseBox": 300_000,
    "CheesePallet": 300_000,
    "CornPallet": 300_000,
    "Container_20ft_01": 300_000,
    "Fuel": 200_000,
    "Coal": 200_000,
    "Container_40ft_01": 300_000,
    "MeatBox": 300_000,
    "ToyBoxes": 300_000,
    "Moonshine": 500_000,
}

# Minimum meaningful distance (metres).  Deliveries below this are
# skipped for distance-based checks to avoid division noise.
MIN_DISTANCE_METRES = 500

# Per-passenger-type payment ceilings (before bonus additions).
# Derived from p99 of legitimate deliveries (excluding known cheats).
PASSENGER_PAYMENT_CEILINGS: dict[int, int] = {
    1: 1_000,  # Hitchhiker
    2: 200_000,  # Taxi (comfort/urgent bonuses can push higher)
    3: 200_000,  # Ambulance
}

# Tow request ceiling.
TOW_PAYMENT_CEILING = 200_000


@dataclass
class FraudFlag:
    """A single fraud detection result."""

    cargo_key: str
    payment: int
    quantity: int
    per_unit: float
    per_unit_threshold: float
    per_km: float | None
    per_km_threshold: float | None
    distance_m: float | None
    excess: int
    reason: str


def _geographic_distance_m(p1: Point, p2: Point) -> float:
    """Compute geographic distance in metres between two SRID-3857 points."""
    p1_wgs = p1.transform(4326, clone=True)
    p2_wgs = p2.transform(4326, clone=True)
    # Haversine approximation — good enough for fraud detection.
    lon1, lat1 = math.radians(p1_wgs.x), math.radians(p1_wgs.y)  # type: ignore[attr-defined]
    lon2, lat2 = math.radians(p2_wgs.x), math.radians(p2_wgs.y)  # type: ignore[attr-defined]
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


async def validate_cargo_payment(
    cargo_key: str,
    payment: int,
    quantity: int,
    sender_point: DeliveryPoint | None,
    destination_point: DeliveryPoint | None,
) -> int:
    """Validate a single cargo delivery payment against baselines.

    Returns the excess amount to claw back (0 if legitimate).
    """
    if payment <= 0 or quantity <= 0:
        return 0

    per_unit = payment / quantity
    per_unit_threshold = CARGO_PER_UNIT_THRESHOLDS.get(cargo_key, CARGO_PER_UNIT_DEFAULT)
    per_km_threshold = CARGO_PER_KM_THRESHOLDS.get(cargo_key)
    max_absolute = CARGO_MAX_ABSOLUTE_PAYMENT.get(cargo_key)

    # --- Distance-based check ---
    distance_m: float | None = None
    per_km: float | None = None
    distance_excess = 0

    if sender_point and destination_point and per_km_threshold is not None:
        sp = sender_point.coord
        dp = destination_point.coord
        if sp and dp and sp.srid and dp.srid:
            distance_m = _geographic_distance_m(sp, dp)
            if distance_m > MIN_DISTANCE_METRES:
                distance_km = distance_m / 1000.0
                per_km = payment / distance_km
                if per_km > per_km_threshold:
                    excess_per_km = per_km - per_km_threshold
                    distance_excess = int(excess_per_km * distance_km)

    # --- Per-unit check ---
    unit_excess = 0
    if per_unit_threshold is not None and per_unit > per_unit_threshold:
        excess_per_unit = per_unit - per_unit_threshold
        unit_excess = int(excess_per_unit * quantity)

    # --- Absolute ceiling check ---
    absolute_excess = 0
    if max_absolute is not None and payment > max_absolute:
        absolute_excess = payment - int(max_absolute)

    # Use the most conservative (largest) clawback.
    excess = max(distance_excess, unit_excess, absolute_excess)

    if excess > 0:
        reason_parts = []
        if distance_excess > 0 and per_km is not None:
            reason_parts.append(
                f"per_km={per_km:.0f} > threshold={per_km_threshold:.0f}"
            )
        if unit_excess > 0:
            reason_parts.append(
                f"per_unit={per_unit:.0f} > threshold={per_unit_threshold:.0f}"
            )
        if absolute_excess > 0:
            reason_parts.append(
                f"payment={payment} > max={int(max_absolute)}"  # type: ignore[arg-type]
            )
        reason = "; ".join(reason_parts)
        logger.warning(
            "FRAUD cargo=%s player=%s payment=%d qty=%d dist=%.0fm "
            "per_unit=%.0f per_km=%s excess=%d reason=[%s]",
            cargo_key,
            "unknown",
            payment,
            quantity,
            distance_m or 0,
            per_unit,
            f"{per_km:.0f}" if per_km else "n/a",
            excess,
            reason,
        )

    return excess


def validate_passenger_payment(passenger_type: int, base_payment: int) -> int:
    """Validate a passenger payment against type-specific ceiling.

    Returns the excess amount to claw back (0 if legitimate).
    """
    if base_payment <= 0:
        return 0

    ceiling = PASSENGER_PAYMENT_CEILINGS.get(passenger_type)
    if ceiling is None:
        return 0

    if base_payment > ceiling:
        excess = base_payment - ceiling
        logger.warning(
            "FRAUD passenger_type=%s payment=%d ceiling=%d excess=%d",
            passenger_type,
            base_payment,
            ceiling,
            excess,
        )
        return excess

    return 0


def validate_tow_payment(payment: int) -> int:
    """Validate a tow request payment against ceiling.

    Returns the excess amount to claw back (0 if legitimate).
    """
    if payment <= 0:
        return 0

    if payment > TOW_PAYMENT_CEILING:
        excess = payment - TOW_PAYMENT_CEILING
        logger.warning(
            "FRAUD tow payment=%d ceiling=%d excess=%d",
            payment,
            TOW_PAYMENT_CEILING,
            excess,
        )
        return excess

    return 0
