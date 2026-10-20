"""Cargo definition rows for payment analysis.

Source of truth: `src/amc/data/cargo_definitions.json`, generated from the
game PAK extraction (vanilla) and the Schedule I supply-chain mod config
(illicit cargo — NOT in the vanilla `cargos` table, see the 2026-10-20
Moonshine forensics).  Regenerate when the game version or mod config
changes; keys are the cargo keys as they appear in webhook events
(`Net_CargoKey`).

Access via `get_cargo_row` — returns None for unknown cargo keys so callers
fail SAFE (skip the check rather than assume pay 0 or infinity).

NOT yet wired into fraud detection: this module only makes the data
available.  A formula-derived detector ceiling built on these rows is a
follow-up gated on per-cargo-family verification (weight-normalization
differences observed between Moonshine and tanker rows).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

_DATA_FILE = Path(__file__).parent / "data" / "cargo_definitions.json"

with open(_DATA_FILE) as _f:
    _RAW: dict[str, Any] = json.load(_f)

VANILLA_ROWS: dict[str, dict[str, Any]] = _RAW["vanilla"]
SCHEDULE_I_ROWS: dict[str, dict[str, Any]] = _RAW["schedule_i"]
GAME_VERSION: str = _RAW.get("game_version", "unknown")

# Mod rows OVERRIDE vanilla rows on key collision (a mod can legitimately
# re-price a vanilla key); lookup order: schedule_i, then vanilla.
CARGO_ROWS: dict[str, dict[str, Any]] = {**VANILLA_ROWS, **SCHEDULE_I_ROWS}


def get_cargo_row(cargo_key: str) -> Optional[dict[str, Any]]:
    """Return the cargo definition row, or None for unknown keys.

    Callers MUST treat None as "no verified data" and skip any check that
    would otherwise assume a pay bound — a missing row must never become a
    0 or infinite expected payment.
    """
    return CARGO_ROWS.get(cargo_key)


def cargo_is_known(cargo_key: str) -> bool:
    return cargo_key in CARGO_ROWS