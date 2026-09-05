"""Motor Town engine power/torque model.

Reverse-engineered from the 0.7.19 client pak (engine DataAssets +
VehicleParts/Engines DataTables) and validated against an in-game dyno run
(2026-09-01) to <0.2%:

    FordSmalBlock302_V8_5L_240HP + Intake 201 + Turbocharger_Stage1
      in-game: 412.3 Nm @4364 / 293.0 hp @6212
      model:   413.0 Nm @4355 / 293.2 hp @6216

Composition (r = rpm / MaxRPM):

    torque(rpm) = curve(r) * MaxTorque * GCM_TO_NM * turbo(r) * intake(r)
    power(rpm)  = torque(rpm) * rpm * 2*pi / 60 / 1000        [kW]

- MaxTorque is stored in g*cm and converts with GCM_TO_NM = 1e-4 (the game
  uses g=10, NOT 9.80665; the naive conversion silently inflates every
  engine by ~2%).
- The torque curve X axis is the rpm/MaxRPM ratio. Curves carry an
  overspeed tail key (e.g. (5, 0.175)) past r=1.0; evaluating the curve
  over its FULL key range is load-bearing -- clamping it at r=1.0
  overstates peaks by 30-70%. Only the intake and turbo multipliers are
  evaluated at min(r, 1.0).
- Intake: UNCLAMPED linear ramp eff(r) = 1 + Slope * (r - BaseRPMRatio).
- Turbo: spool ramps from BaseTorqueMultiplier to TorqueMultiplier as
  exhaust energy E(r) = curve(r)*r reaches E_sat = TurbineWeight/160:
      spool = Base + (Boost - Base) * min((E/E_sat)^3, 1)
  E_sat was calibrated on Stage1 (weight 100). Flat-profile turbos
  (Base == Boost: Stage2/3, TT, QT, HeavyDuty Stage2/3, Compound, Eco,
  Dragster) bypass the spool model entirely and are exact scalars.
- EVs: peak power = MotorMaxPower / 10 watts (game unit quirk); the
  ElectricMotor curves give flat torque but the power rating is the
  MotorMaxPower cap.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

GCM_TO_NM = 1e-4  # g=10 (validated). Do NOT "fix" to 9.80665e-5.
KW_TO_HP = 1.34102
W_PER_HP = 745.699872
SWEEP_MAX_RATIO = 1.25  # validated sweep upper bound, in MaxRPM ratios
SWEEP_STEPS = 1000  # 1001 points, matches the validated dyno reproduction

_LINEAR = "RCIM_Linear"
_CONSTANT = "RCIM_Constant"

CurveKey = Sequence[float]  # [time, value, interp, arrive_tangent, leave_tangent]
Curve = Sequence[CurveKey]


def rich_eval(keys: Curve, t: float) -> float:
    """Evaluate a UE FRichCurve (as baked: [time, value, interp, arrive_t, leave_t]).

    Hermite for RCIM_Cubic, lerp for RCIM_Linear, hold for RCIM_Constant.
    Clamps to the end values outside the key range.
    """
    if t <= keys[0][0]:
        return keys[0][1]
    if t >= keys[-1][0]:
        return keys[-1][1]
    for i in range(len(keys) - 1):
        t0, v0, mode, _at0, lt = keys[i]
        t1, v1, _m1, at1, _lt1 = keys[i + 1]
        if t0 <= t <= t1:
            dt = t1 - t0
            if dt <= 0:
                return v0
            a = (t - t0) / dt
            if mode == _LINEAR:
                return v0 + (v1 - v0) * a
            if mode == _CONSTANT:
                return v0
            k10 = lt * dt
            k11 = at1 * dt
            h00 = 2 * a**3 - 3 * a**2 + 1
            h10 = a**3 - 2 * a**2 + a
            h01 = -2 * a**3 + 3 * a**2
            h11 = a**3 - a**2
            return h00 * v0 + h10 * k10 + h01 * v1 + h11 * k11
    return keys[-1][1]


def intake_multiplier(intake: dict | None, r: float) -> float:
    """Unclamped linear intake efficiency at ratio r (pass r already clamped <=1)."""
    if not intake:
        return 1.0
    slope = intake.get("Slope", 0.0) or 0.0
    base = intake.get("BaseRPMRatio", 0.5)
    return 1.0 + slope * (r - base)


def turbo_spool(
    turbo: dict | None, r: float, curve_at: Callable[[float], float]
) -> float:
    """Turbo torque multiplier at ratio r (r clamped <=1).

    Flat-profile turbos (Base == Boost) return the constant directly.
    Ramping ones blend Base -> Boost on (E/E_sat)^3 with E = curve(r)*r and
    E_sat = TurbineWeight/160 (calibrated on Stage1).
    """
    if not turbo or not turbo.get("bIsValid"):
        return 1.0
    base = turbo.get("BaseTorqueMultiplier", 1.0)
    boost = turbo.get("TorqueMultiplier", 1.0)
    if base == boost:
        return base
    esat = (turbo.get("TurbineWeight", 1.0) or 1.0) / 160.0
    if esat <= 0:
        return boost
    e = curve_at(r) * r
    frac = min((e / esat) ** 3, 1.0)
    return base + (boost - base) * frac


@dataclass
class ComputeResult:
    """Peak figures + metadata for one engine/intake/turbo combination."""

    engine_part: str
    engine_asset: str
    fuel: str
    max_rpm: int
    curve: str
    peak_power_hp: float
    peak_power_kw: float
    peak_power_rpm: float
    peak_torque_nm: float
    peak_torque_rpm: float
    intake_part: str | None = None
    turbo_part: str | None = None
    is_ev: bool = False
    ev_power_hp: float | None = None
    mass_kg: float | None = None
    cost: int | None = None
    curve_points: list = field(default_factory=list)  # (rpm, nm, kw) tuples

    def as_dict(self) -> dict:
        out = {
            "engine_part": self.engine_part,
            "engine_asset": self.engine_asset,
            "fuel": self.fuel,
            "max_rpm": self.max_rpm,
            "curve": self.curve,
            "peak_power_hp": round(self.peak_power_hp, 1),
            "peak_power_kw": round(self.peak_power_kw, 1),
            "peak_power_rpm": round(self.peak_power_rpm),
            "peak_torque_nm": round(self.peak_torque_nm, 1),
            "peak_torque_rpm": round(self.peak_torque_rpm),
            "intake_part": self.intake_part,
            "turbo_part": self.turbo_part,
        }
        if self.is_ev:
            out["is_ev"] = True
            out["ev_power_hp"] = round(self.ev_power_hp, 1) if self.ev_power_hp else None
        if self.mass_kg is not None:
            out["mass_kg"] = self.mass_kg
        if self.cost is not None:
            out["cost"] = self.cost
        if self.curve_points:
            out["curve_points"] = [
                [round(rpm), round(nm, 1), round(kw, 1)] for rpm, nm, kw in self.curve_points
            ]
        return out


def compute(
    engine: dict,
    intake: dict | None = None,
    turbo: dict | None = None,
    *,
    engine_part: str = "",
    intake_part: str | None = None,
    turbo_part: str | None = None,
    keep_points: bool = False,
) -> ComputeResult:
    """Compute peak power/torque for one engine (+intake+turbo) combination.

    `engine` is a baked engine-asset dict (curve_keys/MaxTorque/MaxRPM/...),
    `intake`/`turbo` are the baked part structs (may be None).
    """
    fuel: str = engine.get("FuelType") or "Petrol"
    name: str = engine.get("name") or ""
    max_rpm: int = engine["MaxRPM"]
    points: list = []

    if fuel == "Electric":
        watts = (engine.get("MotorMaxPower") or 0.0) / 10.0
        hp = watts / W_PER_HP
        return ComputeResult(
            engine_part=engine_part,
            engine_asset=name,
            fuel=fuel,
            max_rpm=max_rpm,
            curve=str(engine.get("curve") or ""),
            peak_power_hp=hp,
            peak_power_kw=watts / 1000.0,
            peak_power_rpm=0,
            peak_torque_nm=0.0,
            peak_torque_rpm=0,
            intake_part=intake_part,
            turbo_part=turbo_part,
            is_ev=True,
            ev_power_hp=hp,
        )

    keys: Curve = engine["curve_keys"]
    max_torque_nm = engine["MaxTorque"] * GCM_TO_NM

    def curve_at(r: float) -> float:
        return rich_eval(keys, r)

    best_kw = -1.0
    best_kw_rpm = 0.0
    best_nm = -1.0
    best_nm_rpm = 0.0
    for i in range(SWEEP_STEPS + 1):
        r = SWEEP_MAX_RATIO * i / SWEEP_STEPS
        rpm = r * max_rpm
        rc = min(r, 1.0)
        tq = curve_at(r) * max_torque_nm
        tq *= turbo_spool(turbo, rc, curve_at)
        tq *= intake_multiplier(intake, rc)
        kw = tq * rpm * 2.0 * math.pi / 60.0 / 1000.0
        if kw > best_kw:
            best_kw = kw
            best_kw_rpm = rpm
        if tq > best_nm:
            best_nm = tq
            best_nm_rpm = rpm
        if keep_points:
            points.append((rpm, tq, kw))

    return ComputeResult(
        engine_part=engine_part,
        engine_asset=name,
        fuel=fuel,
        max_rpm=max_rpm,
        curve=str(engine.get("curve") or ""),
        peak_power_hp=best_kw * KW_TO_HP,
        peak_power_kw=best_kw,
        peak_power_rpm=best_kw_rpm,
        peak_torque_nm=best_nm,
        peak_torque_rpm=best_nm_rpm,
        intake_part=intake_part,
        turbo_part=turbo_part,
        curve_points=points,
    )
