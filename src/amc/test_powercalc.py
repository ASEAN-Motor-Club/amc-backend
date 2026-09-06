"""powercalc golden/regression tests.

Pins the validated physics so nobody can silently change it:
- the in-game dyno case (Yuuka 2026-09-01) must reproduce <1%
- engine rating checks (named HP) for vanilla + MoreTuning
- eco-turbo flat-scalar behavior, intake ramp, curve eval basics
- search returns known combos and respects filters
No DB needed -- pure computation over the committed snapshot.
"""

import pytest

from powercalc import compute_setup, data_version, model_version, provenance, search
from powercalc import data as pdata
from powercalc.data import intake_part, turbo_part
from powercalc.model import W_PER_HP, intake_multiplier, rich_eval, turbo_spool
from powercalc.vehicle_setup import compute_popup_lines, resolve

DYNO = provenance()["validation"]


def test_versions_present():
    assert model_version() == "1.0.0"
    assert data_version() == "2026.09.2"


def test_golden_dyno_case():
    """The validated in-game dyno case: <1% on values, <20 rpm on peaks."""
    res = compute_setup("SmallBlock_240HP", intake_part="201", turbo_part="Turbocharger_Stage1")
    ig = DYNO["in_game"]
    assert res.peak_power_hp == pytest.approx(ig["peak_power_hp"], rel=0.01)
    assert res.peak_power_rpm == pytest.approx(ig["peak_power_rpm"], abs=20)
    assert res.peak_torque_nm == pytest.approx(ig["peak_torque_nm"], rel=0.01)
    assert res.peak_torque_rpm == pytest.approx(ig["peak_torque_rpm"], abs=20)


def test_rating_vanilla_240hp_stock():
    res = compute_setup("SmallBlock_240HP")
    assert res.peak_power_hp == pytest.approx(238.6, abs=1.0)


def test_rating_mt_tuned_engine():
    res = compute_setup("bandit1250tuned200HP")
    assert res.peak_power_hp == pytest.approx(200.6, abs=1.0)


def test_ev_peak_power():
    res = compute_setup("EVPolestar2DualMotor")
    assert res.is_ev
    assert res.peak_power_hp == pytest.approx(412.4, abs=0.5)


def test_eco_turbo_is_flat_scalar():
    """EcoStage3 = x0.7 flat: peak hp is exactly 0.7x the NA peak (same build)."""
    na = compute_setup("SmallBlock_240HP", intake_part="201")
    eco = compute_setup("SmallBlock_240HP", intake_part="201", turbo_part="Turbocharger_EcoStage3")
    assert eco.peak_power_hp == pytest.approx(na.peak_power_hp * 0.7, rel=0.005)


def test_stage1_spool_saturates_at_power_peak():
    """Stage1 (0.95 -> 1.2) is fully spooled BY the power peak on SmallBlock
    (exhaust energy past E_sat), so the hp ratio at peak is ~1.2 — but the
    ramp itself shows at low rpm (spool factor well below boost there)."""
    na = compute_setup("SmallBlock_240HP", intake_part="201")
    t1 = compute_setup("SmallBlock_240HP", intake_part="201", turbo_part="Turbocharger_Stage1")
    assert t1.peak_power_hp == pytest.approx(na.peak_power_hp * 1.2, rel=0.01)
    # ramp evidence: spool at low rpm is near the base multiplier, not boost
    from powercalc.data import engine_asset

    eng = engine_asset("FordSmalBlock302_V8_5L_240HP")
    keys = eng["curve_keys"]
    struct = turbo_part("Turbocharger_Stage1")
    assert struct is not None
    low = turbo_spool(struct, 0.25, lambda r: rich_eval(keys, r))
    assert low < 1.05  # base is 0.95; still far from the 1.2 boost


def test_intake_ramp_values():
    short = intake_part("201")
    long_i = intake_part("202")
    assert short is not None and long_i is not None
    assert intake_multiplier(short, 0.7) == pytest.approx(1.0)
    assert intake_multiplier(short, 1.0) == pytest.approx(1.03)
    assert intake_multiplier(long_i, 1.0) == pytest.approx(0.98)


def test_curve_eval_clamps_and_shapes():
    smallblock = [
        [0.0, 0.6, "RCIM_Cubic", 1.7592765, 1.7592765],
        [0.1, 0.8, "RCIM_Cubic", 0.99999994, 0.99999994],
        [0.4, 1.0, "RCIM_Cubic", 0.020145819, 0.020145819],
        [1.0, 0.7, "RCIM_Cubic", -0.88066864, -0.88066864],
        [5.0, 0.17500001, "RCIM_Linear", 0, 0],
    ]
    assert rich_eval(smallblock, -1.0) == 0.6
    assert rich_eval(smallblock, 0.4) == 1.0
    # hand-computed Hermite at a=0.5: 0.5*1.0 + 0.125*k10 + 0.5*0.7 - 0.125*k11
    assert rich_eval(smallblock, 0.7) == pytest.approx(0.9176, abs=0.001)
    assert rich_eval(smallblock, 99.0) == 0.17500001


def test_search_finds_known_400_combo():
    hits = search(400, tolerance=4.0)
    keys = {(h.engine_part, h.intake_part, h.turbo_part) for h in hits}
    assert ("30tdi", "ConeIntakeStage3", "Turbocharger_Stage2") in keys
    hit = next(
        h
        for h in hits
        if (h.engine_part, h.intake_part, h.turbo_part)
        == ("30tdi", "ConeIntakeStage3", "Turbocharger_Stage2")
    )
    assert hit.peak_power_hp == pytest.approx(400.0, abs=0.3)
    assert hit.branch == "turbo"


def test_search_respects_branch_and_mass_filters():
    hits = search(400, tolerance=5.0, branch="na", categories=["car"], min_mass=100, max_mass=600)
    assert hits, "expected NA car hits in the 100-600kg window"
    for h in hits:
        assert h.branch == "na"
        assert h.turbo_part is None
        assert h.category == "car"
        assert 100 < h.mass_kg < 600
    # the stock-rated 400hp V6 must be among them
    assert any(h.engine_part == "V6Sport_400HP" and h.intake_part is None for h in hits)


def test_search_eco_branch_only_eco_turbos():
    hits = search(400, tolerance=4.0, branch="eco")
    assert hits
    for h in hits:
        assert h.turbo_part is not None and h.turbo_part.startswith("Turbocharger_Eco")


def test_search_closeness_sorting():
    hits = search(400, tolerance=4.0, limit=10)
    diffs = [abs(h.peak_power_hp - 400) for h in hits]
    assert diffs == sorted(diffs)


def test_turbo_struct_values():
    t = turbo_part("Turbocharger_Stage2")
    assert t is not None
    assert t["BaseTorqueMultiplier"] == t["TorqueMultiplier"] == 1.275  # flat profile
    eco = turbo_part("Turbocharger_EcoStage3")
    assert eco is not None
    assert eco["TorqueMultiplier"] == 0.7  # "reduced hp" eco branch


# --- vehicle_setup: live parts payload -> /check_parts popup lines --------


def _parts(engine=None, intake=None, turbo=None, extra=None):
    parts = []
    if engine:
        parts.append({"Slot": 2, "Key": engine})
    if intake:
        parts.append({"Slot": 5, "Key": intake})
    if turbo:
        parts.append({"Slot": 7, "Key": turbo})
    if extra:
        parts.extend(extra)
    return parts


def test_vehicle_setup_petrol_exact_matches_compute():
    lines = compute_popup_lines(
        _parts("SmallBlock_240HP", "201", "Turbocharger_Stage1")
    )
    res = compute_setup("SmallBlock_240HP", intake_part="201", turbo_part="Turbocharger_Stage1")
    assert lines[0] == (
        f"Power: {res.peak_power_hp:.1f} hp @ {res.peak_power_rpm:,.0f} rpm"
        f" · {res.peak_torque_nm:.1f} Nm @ {res.peak_torque_rpm:,.0f} rpm"
    )
    # pinned to the validated dyno build (test_golden_dyno_case)
    assert lines[0].startswith("Power: 293.2 hp @ 6,216 rpm")
    assert lines[1] == "Intake 201 · Turbo Turbocharger_Stage1 · engine 250 kg"
    assert lines[2] == f"model {model_version()} · data {data_version()}"


def test_vehicle_setup_na_when_no_turbo_slot():
    lines = compute_popup_lines(_parts("SmallBlock_240HP", "201"))
    res = compute_setup("SmallBlock_240HP", intake_part="201")
    assert lines[0] == (
        f"Power: {res.peak_power_hp:.1f} hp @ {res.peak_power_rpm:,.0f} rpm"
        f" · {res.peak_torque_nm:.1f} Nm @ {res.peak_torque_rpm:,.0f} rpm"
    )
    assert "Turbo none" in lines[1]
    assert "unmodelled" not in lines[0]


def test_vehicle_setup_case_insensitive_keys():
    """Game row-name casing drift is tolerated (same rule as mod_detection)."""
    lines = compute_popup_lines(_parts("smallblock_240hp", "201"))
    assert lines[0].startswith("Power: ")
    assert "unmodelled" not in lines[0]


def test_vehicle_setup_unmodelled_turbo_falls_back_to_na():
    lines = compute_popup_lines(_parts("SmallBlock_240HP", "201", "NotARealTurbo_Stage9"))
    res = compute_setup("SmallBlock_240HP", intake_part="201")
    assert lines[0].startswith("Power: ")
    assert f"{res.peak_power_hp:.1f} hp" in lines[0]
    assert "(unmodelled turbo — NA baseline)" in lines[0]


def test_vehicle_setup_unmodelled_intake_falls_back_to_stock():
    lines = compute_popup_lines(_parts("SmallBlock_240HP", "MysteryIntake_9", None))
    assert "(unmodelled intake — stock baseline)" in lines[0]


def test_vehicle_setup_ev_fixed_rating():
    lines = compute_popup_lines([{"Slot": 2, "Key": "Electric_300HP"}])
    asset = pdata.engine_asset("Electric_300HP")
    expected = (asset["MotorMaxPower"] / 10.0) / W_PER_HP
    assert lines == [f"Power: {expected:.0f} hp (fixed motor rating)"]


def test_vehicle_setup_unknown_engine_is_explicit():
    lines = compute_popup_lines(_parts("Ghost_Engine_999"))
    assert lines == ["Power: Unknown"]


def test_vehicle_setup_no_engine_slot_is_empty():
    assert compute_popup_lines(_parts(intake="201")) == []
    assert compute_popup_lines([]) == []


def test_vehicle_setup_resolve_flags():
    vs = resolve(_parts("SmallBlock_240HP", "201", "Turbocharger_Stage1"))
    assert vs.engine_key == "SmallBlock_240HP"
    assert vs.engine_known and vs.intake_known and vs.turbo_known
    assert not vs.is_ev
    ev = resolve([{"Slot": 2, "Key": "Electric_300HP"}])
    assert ev.is_ev and ev.engine_known
