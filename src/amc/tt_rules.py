"""Time-trial class rules — the parts limitation evaluated per event.

A time-trial event carries a :class:`~amc.models.TTClass` (a power tier).
Eligibility is evaluated from the SAME data path the silent parts audit
(``amc/parts_audit.py``) uses — the mod's minimal parts payload
(``{ID, Key, Slot, Damage}``) resolved through powercalc:

* **Engine power** — ``compute_peak_hp`` must not exceed the class's
  ``max_hp`` plus :data:`HP_BUFFER` (5 hp, per Yuuka's spec: "400hp with
  5hp buffer").
* **Tires** — every Tire-slot part (slots 19..38, ``VehiclePartSlot``)
  must be vanilla. Mod/unknown tire keys are detected with the same
  two-layer detection the ``/check_parts`` popup uses
  (:func:`amc.mod_detection.detect_custom_parts`).

The evaluator NEVER raises and never fabricates: an unresolvable engine is
an explicit violation ("Power: unknown"), not a silent pass — an event that
cannot verify a car must not rubber-stamp it (enforcement is a kick at
event start, so a false pass would be worse than a false fail).
"""

from __future__ import annotations

from amc.mod_detection import detect_custom_parts
from powercalc.vehicle_setup import compute_peak_hp

# Yuuka 2026-09-24: "engine parts are regulated to max hp value (e.g. 400hp
# with 5 hp buffer)".
HP_BUFFER = 5

# VehiclePartSlot.Tire0..TireMax (same plain-int range as parts_audit.py).
TT_TIRE_SLOT_MIN = 19
TT_TIRE_SLOT_MAX = 38


def evaluate_tt_parts(parts: list[dict], max_hp: int) -> list[str]:
    """Return the list of rule violations for one vehicle's parts payload.

    Empty list == compliant. Never raises; every degradation path is an
    explicit violation string so enforcement (event-start kick, step 3)
    can decide policy per violation kind.
    """
    violations: list[str] = []

    peak_hp = compute_peak_hp(parts)
    if peak_hp is None:
        violations.append("Engine power could not be verified (unknown parts)")
    elif peak_hp > max_hp + HP_BUFFER:
        violations.append(
            f"Engine power {peak_hp:.0f}hp exceeds the {max_hp}hp class "
            f"limit (+{HP_BUFFER} buffer)"
        )

    mod_tires = [
        part
        for part in detect_custom_parts(parts)
        if TT_TIRE_SLOT_MIN <= int(part.get("slot_value", 0)) <= TT_TIRE_SLOT_MAX
    ]
    if mod_tires:
        keys = sorted({str(part.get("key") or "?") for part in mod_tires})
        violations.append("Non-vanilla tires: " + ", ".join(keys))

    return violations
