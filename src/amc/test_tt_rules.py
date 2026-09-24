"""Tests for TT class rules (tt_rules) and the per-instance class tag."""

from amc.tt_rules import HP_BUFFER, evaluate_tt_parts


def _parts(engine="SmallBlock_240HP", extra=None):
    parts = [{"Slot": 2, "Key": engine}] if engine else []
    parts += extra or []
    return parts


def test_compliant_stock_build_under_class_limit():
    # SmallBlock_240HP peaks ~238.6hp — compliant in the 270 class.
    assert evaluate_tt_parts(_parts(), max_hp=270) == []


def test_over_power_engine_violates_with_buffer():
    # 240hp build in the 140 class: 238.6 > 140 + 5.
    violations = evaluate_tt_parts(_parts(), max_hp=140)
    assert any("exceeds the 140hp class" in v for v in violations)


def test_buffer_tolerates_borderline_power():
    # 238.6hp in a 240 class: within +HP_BUFFER -> no power violation.
    violations = evaluate_tt_parts(_parts(), max_hp=240)
    assert not any("exceeds" in v for v in violations)


def test_unknown_engine_power_is_explicit_violation():
    # An unmodelled engine must NOT pass silently.
    violations = evaluate_tt_parts(
        _parts(engine="Definitely_Not_Modelled_900HP"), max_hp=480
    )
    assert any("could not be verified" in v for v in violations)


def test_mod_tire_is_violation():
    # More Tuning Baja tire (known_mod_parts registry, not a vanilla key) on
    # a tire slot.
    violations = evaluate_tt_parts(
        _parts(extra=[{"Slot": 19, "Key": "HeavyDutyBaja60FrontTire"}]), max_hp=480
    )
    assert any("Non-vanilla tires" in v for v in violations)


def test_vanilla_tires_pass():
    assert evaluate_tt_parts(_parts(extra=[{"Slot": 19, "Key": "201"}]), max_hp=480) == []


def test_buffer_constant_is_five():
    assert HP_BUFFER == 5
