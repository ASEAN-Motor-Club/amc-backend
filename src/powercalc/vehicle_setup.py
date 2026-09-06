"""Resolve a live vehicle parts payload to powercalc inputs.

The mod's parts endpoint (``/player_vehicles/{guid}/last/parts``) returns
minimal dicts ``{ID, Key, Slot, Damage}`` where ``Key`` is the VehicleParts
DataTable row name — the same id space as the powercalc snapshot
(``SmallBlock_240HP``, ``201``, ``Turbocharger_Stage1``, ... verified against
gamedata.db). This module maps the installed parts onto
:func:`powercalc.compute_setup` and renders popup-ready text lines.

Slot numbers mirror ``amc.vehicles.VehiclePartSlot`` (Engine=2, Intake=5,
Turbocharger=7) and are kept as plain ints so this package stays Django-free.

Unknown ids degrade to explicit "Unknown" output — never fabricated numbers
and never a silently wrong baseline (unmodelled intake/turbo falls back to
stock/NA but says so).
"""

from __future__ import annotations

from dataclasses import dataclass

from powercalc import PartNotFound, compute_setup, data_version, model_version
from powercalc import data as pdata
from powercalc.model import W_PER_HP

SLOT_ENGINE = 2
SLOT_INTAKE = 5
SLOT_TURBO = 7


@dataclass
class VehicleSetup:
    """Resolved installed-setup view of one vehicle's parts payload."""

    engine_key: str | None = None
    intake_key: str | None = None
    turbo_key: str | None = None
    engine_known: bool = False
    intake_known: bool = True  # absent intake slot == stock intake == known
    turbo_known: bool = True  # absent turbo slot == naturally aspirated == known
    is_ev: bool = False


def resolve(parts: list[dict]) -> VehicleSetup:
    """Map a parts payload (``Key``/``Slot`` dicts) to powercalc inputs.

    Lookup is exact first, then case-insensitive (row-name casing drift,
    same reason ``mod_detection`` lowercases). A vanilla ``Electric_*`` part
    is not in ``engine_parts`` but is an engine *asset* with
    ``FuelType="Electric"`` — probed second, flagged ``is_ev``.
    """
    by_slot: dict[int, str] = {}
    for part in parts:
        key = part.get("Key")
        if key:
            by_slot[part.get("Slot", 0)] = key

    vs = VehicleSetup(
        engine_key=by_slot.get(SLOT_ENGINE),
        intake_key=by_slot.get(SLOT_INTAKE),
        turbo_key=by_slot.get(SLOT_TURBO),
    )
    if not vs.engine_key:
        return vs

    try:
        pdata.engine_part(vs.engine_key)
        vs.engine_known = True
    except PartNotFound:
        vs.engine_known = _engine_known_ci(vs.engine_key, vs)

    if vs.intake_key and not _part_known(vs.intake_key, "Intake"):
        vs.intake_known = False
    if vs.turbo_key and not _part_known(vs.turbo_key, "Turbocharger"):
        vs.turbo_known = False
    return vs


def _engine_known_ci(key: str, vs: VehicleSetup) -> bool:
    """Case-insensitive engine retry, then the EV asset probe."""
    lower = {k.lower(): k for k in pdata._snapshot()["engine_parts"]}
    hit = lower.get(key.lower())
    if hit:
        vs.engine_key = hit
        return True
    try:
        asset = pdata.engine_asset(key)
    except PartNotFound:
        return False
    if (asset.get("FuelType") or "").lower() == "electric":
        vs.is_ev = True
        vs.engine_known = True
        return True
    return False


def _part_known(key: str, ptype: str) -> bool:
    lower = {
        k.lower()
        for k, v in pdata._snapshot()["parts"].items()
        if v.get("type") == ptype
    }
    return key.lower() in lower


def compute_popup_lines(parts: list[dict]) -> list[str]:
    """Power-block lines for the /check_parts popup.

    Never raises and never fabricates: an engine outside the model data
    yields exactly ``["Power: Unknown"]``; a vehicle without an engine slot
    yields ``[]`` (the block is omitted). Unmodelled intake/turbo compute a
    stock/NA baseline with an explicit note.
    """
    vs = resolve(parts)
    if not vs.engine_key:
        return []
    if not vs.engine_known:
        return ["Power: Unknown"]
    try:
        if vs.is_ev:
            asset = pdata.engine_asset(vs.engine_key)
            watts = (asset.get("MotorMaxPower") or 0.0) / 10.0
            hp = watts / W_PER_HP
            return [f"Power: {hp:.0f} hp (fixed motor rating)"]

        result = compute_setup(
            vs.engine_key,
            vs.intake_key if vs.intake_known else None,
            vs.turbo_key if vs.turbo_known else None,
        )
    except PartNotFound:
        return ["Power: Unknown"]

    note = ""
    if not vs.intake_known:
        note += " (unmodelled intake — stock baseline)"
    if not vs.turbo_known:
        note += " (unmodelled turbo — NA baseline)"
    lines = [
        (
            f"Power: {result.peak_power_hp:.1f} hp @ "
            f"{result.peak_power_rpm:,.0f} rpm · "
            f"{result.peak_torque_nm:.1f} Nm @ "
            f"{result.peak_torque_rpm:,.0f} rpm{note}"
        ),
        (
            f"Intake {result.intake_part or 'stock'}"
            f" · Turbo {result.turbo_part or 'none'}"
        ),
        f"model {model_version()} · data {data_version()}",
    ]
    if result.mass_kg:
        lines[1] += f" · engine {result.mass_kg:.0f} kg"
    return lines
