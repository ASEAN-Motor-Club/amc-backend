"""Vehicle + per-part weight data for /check_parts and the parts audit.

All numbers come from the pak-derived gamedata snapshot plus the committed
powercalc snapshot — no fabrication, explicit degradation everywhere:

* ``vehicle_weights.chassis_mass_kg`` — per-vehicle base mass. Joined by the
  blueprint class name derived from the mod's vehicle ``fullName``
  (``"Elisa2_C Default__Elisa2"`` -> ``Elisa2``). The column's
  ``blueprint_path`` ships in two shapes (``Default__X_C`` and bare ``X_C``);
  both normalise to the same key. ~138 of 168 vehicles carry a weight —
  police/taxi/hidden variants mostly don't and degrade to ``Unknown``.
* ``vehicle_parts.mass_kg`` — per-part mass for non-engine parts. Stock
  default parts are authored at 0 kg; aftermarket parts carry real masses.
  Tuned variant keys (``Damper200_200``) fall back to their base row — but
  ONLY for the tuning-suffix families in ``mod_detection.SUFFIX_RULES``; a
  blind strip would mis-resolve e.g. tire ``201_50`` onto intake ``201``.
* Engine masses come from the committed powercalc snapshot
  (``engine_parts[key]["mass_kg"]``) — engines are DataAssets, not
  VehicleParts rows, so they never appear in the parts table. EV motors have
  no mass in the model data and count as uncounted.

The total is COMPUTED (chassis + sum of installed part masses), not measured
in-game. Whether the game's simulated mass composes exactly this way is
unverified; the lines therefore label the total and degrade explicitly —
unknown chassis or unresolved part masses are never silently folded in
(uncounted parts are shown as a count, keeping the total an explicit lower
bound).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass

from powercalc import PartNotFound
from powercalc import data as pdata

log = logging.getLogger(__name__)

GAME_DB_PATH = os.environ.get("GAME_DB_PATH", "/var/lib/motortown/gamedata.db")

# Part types whose player keys are generated ``<base>_<value>`` tuned
# variants of a base row (mod_detection.SUFFIX_RULES). Only these may use
# the base-row mass fallback — a blind strip would mis-resolve unrelated
# underscored keys (tire ``201_50`` -> intake ``201``).
_TUNING_FAMILIES = frozenset(
    {"Suspension_Damper", "BrakePower", "CoolantRadiator", "WheelSpacer"}
)


@dataclass
class WeightSummary:
    """Computed weight view of one vehicle + parts payload."""

    chassis_kg: float | None = None
    parts_kg: float = 0.0  # sum of RESOLVED part masses (0 legitimately = all stock-0)
    resolved: int = 0
    uncounted: int = 0  # installed parts whose mass is unknown (mod parts, EV motor)
    total_kg: float | None = None  # chassis + parts, only when chassis is known


# --- cached gamedata loaders (same pattern as mod_detection.get_final_drive_ratios)

_chassis_by_key: dict[str, float] | None = None
_part_masses: dict[str, float] | None = None
_part_mass_bases: dict[str, float] | None = None


def reset_caches() -> None:
    """Drop cached loaders (tests, or after a gamedata regen)."""
    global _chassis_by_key, _part_masses, _part_mass_bases
    _chassis_by_key = None
    _part_masses = None
    _part_mass_bases = None


def blueprint_key(vehicle_full_name: str) -> str:
    """``"Elisa2_C Default__Elisa2"`` -> ``"Elisa2"`` (first token, no _C)."""
    token = (vehicle_full_name or "").split(" ")[0]
    return token.removesuffix("_C")


def _load_chassis_masses() -> dict[str, float]:
    """Map of normalised blueprint key -> chassis mass kg, cached."""
    global _chassis_by_key
    if _chassis_by_key is not None:
        return _chassis_by_key
    out: dict[str, float] = {}
    try:
        conn = sqlite3.connect(f"file:{GAME_DB_PATH}?mode=ro", uri=True, timeout=5)
        try:
            for path, mass in conn.execute(
                "SELECT blueprint_path, chassis_mass_kg FROM vehicle_weights "
                "WHERE chassis_mass_kg IS NOT NULL"
            ):
                key = (path or "").removeprefix("Default__").removesuffix("_C")
                if key and mass > 0:
                    out[key.lower()] = float(mass)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — degrade to empty, display falls back to Unknown
        log.error("Failed to load vehicle chassis masses: %s", e)
    _chassis_by_key = out
    return out


def _load_part_masses() -> tuple[dict[str, float], dict[str, float]]:
    """(all part masses, tuning-family base-row masses), cached, lowercased."""
    global _part_masses, _part_mass_bases
    if _part_masses is not None and _part_mass_bases is not None:
        return _part_masses, _part_mass_bases
    exact: dict[str, float] = {}
    bases: dict[str, float] = {}
    try:
        conn = sqlite3.connect(f"file:{GAME_DB_PATH}?mode=ro", uri=True, timeout=5)
        try:
            for name, ptype, mass in conn.execute(
                "SELECT name, part_type, mass_kg FROM vehicle_parts "
                "WHERE name IS NOT NULL AND mass_kg IS NOT NULL"
            ):
                exact[str(name).lower()] = float(mass)
                if ptype in _TUNING_FAMILIES:
                    bases[str(name).lower()] = float(mass)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — degrade to empty
        log.error("Failed to load part masses: %s", e)
    _part_masses = exact
    _part_mass_bases = bases
    return _part_masses, _part_mass_bases


def _engine_mass(key: str) -> float | None:
    """Engine part mass from the powercalc snapshot (engines are DataAssets)."""
    try:
        return pdata.engine_part(key).get("mass_kg")
    except PartNotFound:
        pass
    lower = {k.lower(): v.get("mass_kg") for k, v in pdata._snapshot()["engine_parts"].items()}
    return lower.get(key.lower())


def part_mass(key: str | None, slot: int | None = None) -> float | None:
    """Mass (kg) of one installed part, or None when unknown.

    Order: exact VehicleParts row -> tuning-family base row -> powercalc
    engine part (engine slot only; engine rows are not in the VehicleParts
    table).
    """
    if not key:
        return None
    exact, bases = _load_part_masses()
    k = key.lower()
    if k in exact:
        return exact[k]
    base = k.rsplit("_", 1)[0]
    if base != k and base in bases:
        return bases[base]
    if slot == 2:
        return _engine_mass(key)
    return None


def chassis_mass(vehicle_full_name: str) -> float | None:
    """Base chassis mass (kg) for a vehicle ``fullName``, or None."""
    return _load_chassis_masses().get(blueprint_key(vehicle_full_name).lower())


def compute_weight_summary(vehicle_full_name: str, parts: list[dict]) -> WeightSummary:
    """Chassis + installed-part masses; unknowns counted, never folded in."""
    chassis = chassis_mass(vehicle_full_name)
    parts_sum = 0.0
    resolved = 0
    uncounted = 0
    for part in parts:
        key = part.get("Key")
        if not key:
            continue
        m = part_mass(key, part.get("Slot"))
        if m is None:
            uncounted += 1
        else:
            resolved += 1
            parts_sum += m
    total = (chassis + parts_sum) if chassis is not None else None
    return WeightSummary(
        chassis_kg=chassis,
        parts_kg=parts_sum,
        resolved=resolved,
        uncounted=uncounted,
        total_kg=total,
    )


def _fmt_kg(v: float) -> str:
    return f"{v:,.0f}"


def format_weight_lines(summary: WeightSummary, peak_hp: float | None) -> list[str]:
    """Popup-style weight block. Never fabricates a total.

    * chassis + all masses known -> ``Weight: 1,670 kg (1,420 chassis + 250 parts)``
    * unresolved part masses     -> lower bound, ``N uncounted`` called out
    * chassis unknown            -> ``Weight: Unknown`` (+ parts mass when > 0)
    * PWR only when BOTH total and peak HP exist
    """
    lines: list[str] = []
    parts = _fmt_kg(summary.parts_kg)
    total = summary.total_kg
    if summary.chassis_kg is not None:
        if total is None:
            total = summary.chassis_kg + summary.parts_kg
        base = (
            f"Weight: {_fmt_kg(total)} kg "
            f"({_fmt_kg(summary.chassis_kg)} chassis + {parts} parts"
        )
        if summary.uncounted:
            lines.append(base + f", {summary.uncounted} uncounted)")
        else:
            lines.append(base + ")")
    elif summary.parts_kg > 0:
        note = f" + {parts} kg parts"
        if summary.uncounted:
            note += f", {summary.uncounted} uncounted"
        lines.append(f"Weight: Unknown chassis{note}")
    else:
        lines.append("Weight: Unknown")

    if summary.total_kg and peak_hp:
        pwr = peak_hp / (summary.total_kg / 1000.0)
        lines.append(f"PWR: {pwr:.0f} hp/t")
    return lines


def weight_popup_lines(
    vehicle_full_name: str, parts: list[dict], peak_hp: float | None
) -> list[str]:
    """One-shot popup lines: compute summary + render."""
    return format_weight_lines(compute_weight_summary(vehicle_full_name, parts), peak_hp)


def audit_weight_line(
    vehicle_full_name: str, parts: list[dict], peak_hp: float | None
) -> str | None:
    """Single lean line for the Discord audit embed, or None when no data."""
    summary = compute_weight_summary(vehicle_full_name, parts)
    lines = format_weight_lines(summary, peak_hp)
    if not lines:
        return None
    return " · ".join(lines)
