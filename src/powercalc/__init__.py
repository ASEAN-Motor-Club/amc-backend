"""powercalc -- Motor Town engine power/torque calculator.

Validated model + baked data snapshot (vanilla 0.7.19 + MoreTuning).
Public API:

    from powercalc import compute_setup, search, list_parts, data_version

    # "calculate setup": exact combination -> full result
    result = compute_setup("SmallBlock_240HP", intake_part="201",
                           turbo_part="Turbocharger_Stage1")

    # "recommend": target hp -> ranked combinations
    hits = search(400, branch="eco", min_mass=100, max_mass=600)

Data ships as a committed snapshot (powercalc/data/); regenerate via the
mt-pak-extract pipeline when the game or MoreTuning updates -- see
data/provenance.json for sources and the validation case.
"""

from __future__ import annotations

from collections.abc import Iterable

from . import data as _data
from .model import ComputeResult, compute
from .search import SearchHit
from .search import search as _search_impl

__all__ = [
    "ComputeResult",
    "PartNotFound",
    "SearchHit",
    "compute_setup",
    "data_version",
    "list_engines",
    "list_parts",
    "model_version",
    "provenance",
    "search",
]

PartNotFound = _data.PartNotFound


def compute_setup(
    engine_part: str,
    intake_part: str | None = None,
    turbo_part: str | None = None,
    keep_points: bool = False,
) -> ComputeResult:
    """Calculate an exact setup ("calculate setup" mode).

    engine_part: engine part id from the Engines DataTable (e.g.
    "SmallBlock_240HP"; modded rows use their row id). intake_part: intake
    part id or None for stock intake. turbo_part: turbo part id or None for
    naturally aspirated.
    """
    row, engine = _data.resolve_engine(engine_part)
    intake = _data.intake_part(intake_part) if intake_part else None
    turbo = _data.turbo_part(turbo_part) if turbo_part else None
    result = compute(
        engine,
        intake,
        turbo,
        engine_part=engine_part,
        intake_part=intake_part,
        turbo_part=turbo_part,
        keep_points=keep_points,
    )
    result.mass_kg = row.get("mass_kg")
    result.cost = row.get("cost")
    return result


def search(
    target_hp: float,
    tolerance: float = 4.0,
    branch: str | None = None,
    categories: Iterable[str] | None = None,
    min_mass: float | None = None,
    max_mass: float | None = None,
    limit: int = 50,
    sort: str = "closeness",
) -> list[SearchHit]:
    """Recommend combinations peaking near target_hp ("target hp" mode).

    branch: None/all, "na", "turbo", "eco", "ev". categories: subset of
    (car, pickup, truck, bike); None = all. See search.search for details.
    """
    return _search_impl(
        target_hp,
        tolerance=tolerance,
        branch=branch,
        categories=categories,
        min_mass=min_mass,
        max_mass=max_mass,
        limit=limit,
        sort=sort,
    )


def list_parts() -> dict:
    """All intake + turbo parts with their baked struct values."""
    snap = _data._snapshot()
    out: dict[str, dict] = {"Intake": {}, "Turbocharger": {}}
    for pid, row in sorted(snap["parts"].items()):
        entry = {k: v for k, v in row.items() if k != "type"}
        out.setdefault(str(row.get("type")), {})[pid] = entry
    return out


def list_engines(
    category: str | None = None,
    min_mass: float | None = None,
    max_mass: float | None = None,
) -> list[dict]:
    """Engine part rows (id, asset, mass, category, cost) matching filters."""
    out = []
    for pid in _data.engine_part_ids(category, min_mass, max_mass):
        row = _data.engine_part(pid)
        out.append(
            {
                "engine_part": pid,
                "engine_asset": row.get("engine_asset"),
                "mass_kg": row.get("mass_kg"),
                "category": row.get("category"),
                "cost": row.get("cost"),
            }
        )
    return out


def data_version() -> str:
    return _data.data_version()


def model_version() -> str:
    return _data.model_version()


def provenance() -> dict:
    return _data.provenance()
