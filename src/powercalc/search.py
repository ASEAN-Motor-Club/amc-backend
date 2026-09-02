"""Constrained combination search ("recommend a build for N hp").

Two-phase evaluation for speed in pure stdlib:

1. Grid pass: every candidate combination is sampled on a coarse rpm-ratio
   grid (81 points over [0, 1.25]) using precomputed per-engine curve
   samples. Flat-profile turbos (Base == Boost, 22 of the 27 known turbos)
   are exact scalars on torque, so their peak is the NA peak times the
   scalar -- one grid pass per (engine, intake) covers all of them.
2. Refine pass: the argmax neighborhood is refined with the exact model
   (full FRichCurve eval, ternary search) so reported peaks match the
   1001-point validated sweep to <0.2%.

Only ramping turbos (Stock/Stage1 family, 5 of 27) pay the full per-combo
grid cost; the spool blend reuses a precomputed (curve(r)*r)^3 grid per
engine.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from . import data as pdata
from .model import (
    GCM_TO_NM,
    KW_TO_HP,
    SWEEP_MAX_RATIO,
    W_PER_HP,
    intake_multiplier,
    rich_eval,
    turbo_spool,
)

_GRID_STEPS = 80  # 81 points over [0, 1.25]
_RPM2KW = 2.0 * math.pi / 60.0 / 1000.0

Branch = str | None  # None | "na" | "turbo" | "eco" | "ev"


@dataclass(frozen=True)
class SearchHit:
    engine_part: str
    engine_asset: str
    intake_part: str | None
    turbo_part: str | None
    branch: str
    peak_power_hp: float
    peak_torque_nm: float
    peak_power_rpm: float
    peak_torque_rpm: float
    mass_kg: float
    category: str
    cost: int

    def as_dict(self) -> dict:
        return {
            "engine_part": self.engine_part,
            "engine_asset": self.engine_asset,
            "intake_part": self.intake_part,
            "turbo_part": self.turbo_part,
            "branch": self.branch,
            "peak_power_hp": round(self.peak_power_hp, 1),
            "peak_torque_nm": round(self.peak_torque_nm, 1),
            "peak_power_rpm": round(self.peak_power_rpm),
            "peak_torque_rpm": round(self.peak_torque_rpm),
            "mass_kg": self.mass_kg,
            "category": self.category,
            "cost": self.cost,
        }


def _grid():
    step = SWEEP_MAX_RATIO / _GRID_STEPS
    rs = [step * i for i in range(_GRID_STEPS + 1)]
    rmin = [min(r, 1.0) for r in rs]
    return rs, rmin


_RS, _RMIN = _grid()

# ---- lazy per-engine caches -------------------------------------------------
_curve_grid: dict[str, list[float]] = {}
_e3_grid: dict[str, list[float]] = {}
_na_refine_cache: dict[tuple[str, str], tuple[float, float, float, float]] = {}


def _engine_grids(asset_name: str, engine: dict) -> tuple[list[float], list[float]]:
    """(curve grid, (curve*r)^3 grid) on the search grid, cached per asset."""
    hit = _curve_grid.get(asset_name)
    if hit is None:
        keys = engine["curve_keys"]
        hit = [rich_eval(keys, r) for r in _RS]
        _curve_grid[asset_name] = hit
    if asset_name not in _e3_grid:
        _e3_grid[asset_name] = [(c * r) ** 3 for c, r in zip(hit, _RMIN)]
    return hit, _e3_grid[asset_name]


def _refine(
    engine: dict,
    intake: dict | None,
    turbo: dict | None,
    lo: float,
    hi: float,
) -> tuple[float, float, float, float]:
    """Ternary-search refine of (peak kw, rpm, peak nm, rpm) in [lo, hi] ratios
    using the exact model. Returns (best_kw_rpm, best_kw, best_nm, best_nm_rpm)."""
    keys = engine["curve_keys"]
    max_tq = engine["MaxTorque"] * GCM_TO_NM

    def curve_at(r: float) -> float:
        return rich_eval(keys, r)

    def eval_at(r: float) -> tuple[float, float]:  # (kw, nm)
        rpm = r * engine["MaxRPM"]
        rc = min(r, 1.0)
        tq = curve_at(r) * max_tq
        tq *= turbo_spool(turbo, rc, curve_at)
        tq *= intake_multiplier(intake, rc)
        return tq * rpm * _RPM2KW, tq

    # ternary search on kw (unimodal in practice; the sweep curve is smooth)
    a, b = max(0.0, lo), min(SWEEP_MAX_RATIO, hi)
    for _ in range(40):
        m1 = a + (b - a) / 3.0
        m2 = b - (b - a) / 3.0
        if eval_at(m1)[0] < eval_at(m2)[0]:
            a = m1
        else:
            b = m2
    r_kw = (a + b) / 2.0
    kw, _ = eval_at(r_kw)
    # torque peak: scan the window coarsely then refine (torque is not always
    # unimodal for ramping turbos, so brute force the small window)
    best_nm = -1.0
    best_nm_rpm = 0.0
    steps = 24
    for i in range(steps + 1):
        r = a + (b - a) * i / steps if b > a else a
        _, nm = eval_at(r)
        if nm > best_nm:
            best_nm = nm
            best_nm_rpm = r * engine["MaxRPM"]
    return r_kw * engine["MaxRPM"], kw, best_nm, best_nm_rpm


def _branches_for(branch: Branch) -> list[str]:
    if branch in (None, "all"):
        return ["na", "turbo", "eco", "ev"]
    return [branch]


def search(
    target_hp: float,
    tolerance: float = 4.0,
    branch: Branch = None,
    categories: Iterable[str] | None = None,
    min_mass: float | None = None,
    max_mass: float | None = None,
    limit: int = 50,
    sort: str = "closeness",
) -> list[SearchHit]:
    """Find engine/intake/turbo combinations peaking within tolerance of target_hp.

    branch: None/"all", "na" (no turbo), "turbo" (non-eco turbos), "eco"
    (Eco* turbos), "ev" (electric). categories: engine category filter
    (car/pickup/truck/bike); None = all. min/max_mass filter on engine
    MassKg. sort: "closeness" (|hp-target|, cost, -nm), "torque" (-nm),
    "cost" (cost, |hp-target|).
    """
    wanted = set(_branches_for(branch))
    cat_set = set(categories) if categories else None
    snap = pdata._snapshot()
    parts = snap["parts"]
    turbo_lists: dict[str, list[tuple[str, dict | None]]] = {
        "na": [("(none)", None)],
        "turbo": [],
        "eco": [],
    }
    for pid in sorted(p for p, v in parts.items() if v.get("type") == "Turbocharger"):
        t = parts[pid].get("turbocharger") or {}
        if not t.get("bIsValid"):
            continue
        key = "eco" if "Eco" in pid else "turbo"
        turbo_lists[key].append((pid, t))

    intake_list: list[tuple[str, dict | None]] = [("(stock)", None)]
    for pid in sorted(p for p, v in parts.items() if v.get("type") == "Intake"):
        intake_list.append((pid, parts[pid].get("intake")))

    lo_hp = target_hp - tolerance
    hi_hp = target_hp + tolerance
    hits: list[SearchHit] = []

    for epid, row in sorted(snap["engine_parts"].items()):
        if cat_set is not None and row.get("category") not in cat_set:
            continue
        mass = float(row.get("mass_kg") or 0.0)
        if min_mass is not None and mass < min_mass:
            continue
        if max_mass is not None and mass > max_mass:
            continue
        engine = pdata.engine_asset(row.get("engine_asset") or "")
        if not engine.get("curve_keys"):
            continue
        engine_cost = int(row.get("cost") or 0)
        fuel = engine.get("FuelType") or "Petrol"

        if fuel == "Electric":
            if "ev" not in wanted:
                continue
            hp = (engine.get("MotorMaxPower") or 0.0) / 10.0 / W_PER_HP
            if lo_hp <= hp <= hi_hp:
                hits.append(
                    SearchHit(
                        engine_part=epid,
                        engine_asset=engine.get("name", ""),
                        intake_part=None,
                        turbo_part=None,
                        branch="ev",
                        peak_power_hp=hp,
                        peak_torque_nm=0.0,
                        peak_power_rpm=0,
                        peak_torque_rpm=0,
                        mass_kg=mass,
                        category=row.get("category", ""),
                        cost=engine_cost,
                    )
                )
            continue

        curve_grid, e3_grid = _engine_grids(engine.get("name", ""), engine)
        max_rpm = engine["MaxRPM"]
        base_tq = [c * engine["MaxTorque"] * GCM_TO_NM for c in curve_grid]
        rpm_grid = [r * max_rpm for r in _RS]

        # intake-multiplied no-turbo torque grids, shared by na + flat turbos
        for iname, intake in intake_list:
            if not any(b in wanted for b in ("na", "turbo", "eco")):
                break
            tq0 = [
                tq * intake_multiplier(intake, rc) for tq, rc in zip(base_tq, _RMIN)
            ]
            kw0 = [tq * rp * _RPM2KW for tq, rp in zip(tq0, rpm_grid)]
            i0 = max(range(len(kw0)), key=kw0.__getitem__)
            # window for refine: neighbors of the grid argmax
            wlo = _RS[max(0, i0 - 1)]
            whi = _RS[min(len(_RS) - 1, i0 + 1)]

            for b in ("na", "turbo", "eco"):
                if b not in wanted:
                    continue
                for tname, turbo in turbo_lists[b]:
                    i0t = 0  # grid argmax for ramping turbos (set below)
                    if turbo is None:
                        grid_hp = kw0[i0] * KW_TO_HP
                    elif turbo.get("BaseTorqueMultiplier") == turbo.get("TorqueMultiplier"):
                        grid_hp = kw0[i0] * float(turbo.get("BaseTorqueMultiplier", 1.0)) * KW_TO_HP
                    else:
                        t_base = turbo.get("BaseTorqueMultiplier", 1.0)
                        t_boost = turbo.get("TorqueMultiplier", 1.0)
                        esat = (turbo.get("TurbineWeight", 1.0) or 1.0) / 160.0
                        spool_grid = [
                            t_base
                            + (t_boost - t_base) * min(e3 / esat**3, 1.0)
                            for e3 in e3_grid
                        ]
                        tq = [tq0v * sp for tq0v, sp in zip(tq0, spool_grid)]
                        kw = [tqv * rp * _RPM2KW for tqv, rp in zip(tq, rpm_grid)]
                        i0t = max(range(len(kw)), key=kw.__getitem__)
                        grid_hp = kw[i0t] * KW_TO_HP
                    # cheap reject: grid peaks are within ~1% of the exact
                    # value, so anything outside the padded window can't hit
                    if not (lo_hp * 0.98 <= grid_hp <= hi_hp * 1.02):
                        continue
                    if turbo is None:
                        kw_rpm, kw, nm, nm_rpm = _refine(
                            engine, intake, None, wlo, whi
                        )
                    elif turbo.get("BaseTorqueMultiplier") == turbo.get("TorqueMultiplier"):
                        scalar = turbo.get("BaseTorqueMultiplier", 1.0)
                        # argmax identical to NA; refine once per (engine, intake)
                        # and scale the peaks -- the scalar multiplies torque and
                        # therefore power exactly.
                        ckey = (engine.get("name", ""), iname)
                        cached = _na_refine_cache.get(ckey)
                        if cached is None:
                            cached = _refine(engine, intake, None, wlo, whi)
                            _na_refine_cache[ckey] = cached
                        kw_rpm, kw0r, nm0, nm0_rpm = cached
                        kw = kw0r * scalar
                        nm = nm0 * scalar
                        nm_rpm = nm0_rpm
                    else:
                        wlot = _RS[max(0, i0t - 1)]
                        whit = _RS[min(len(_RS) - 1, i0t + 1)]
                        kw_rpm, kw, nm, nm_rpm = _refine(
                            engine, intake, turbo, wlot, whit
                        )
                    hp = kw * KW_TO_HP
                    if lo_hp <= hp <= hi_hp:
                        hits.append(
                            SearchHit(
                                engine_part=epid,
                                engine_asset=engine.get("name", ""),
                                intake_part=None if iname == "(stock)" else iname,
                                turbo_part=None if tname == "(none)" else tname,
                                branch=b,
                                peak_power_hp=hp,
                                peak_torque_nm=nm,
                                peak_power_rpm=kw_rpm,
                                peak_torque_rpm=nm_rpm,
                                mass_kg=mass,
                                category=row.get("category", ""),
                                cost=engine_cost
                                + (pdata.part_cost(iname) if iname != "(stock)" else 0)
                                + (pdata.part_cost(tname) if tname != "(none)" else 0),
                            )
                        )

    if sort == "torque":
        hits.sort(key=lambda h: (-h.peak_torque_nm, abs(h.peak_power_hp - target_hp)))
    elif sort == "cost":
        hits.sort(key=lambda h: (h.cost, abs(h.peak_power_hp - target_hp), -h.peak_torque_nm))
    else:  # closeness
        hits.sort(
            key=lambda h: (abs(h.peak_power_hp - target_hp), h.cost, -h.peak_torque_nm)
        )
    return hits[:limit]
