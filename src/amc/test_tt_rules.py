"""Tests for TT class rules (tt_rules) and the per-instance class tag."""

from unittest.mock import patch

from amc.models import TTClass
from amc.tt_rules import HP_BUFFER, evaluate_tt_parts


def _parts(engine="SmallBlock_240HP", extra=None):
    parts = [{"Slot": 2, "Key": engine}] if engine else []
    parts += extra or []
    return parts


def _cls(hp: int) -> TTClass:
    """Unsaved TTClass stub — the field default supplies the vehicle types."""
    return TTClass(name=f"TT-{hp}", max_hp=hp)


def test_compliant_stock_build_under_class_limit():
    # SmallBlock_240HP peaks ~238.6hp — compliant in the 270 class.
    assert evaluate_tt_parts(_parts(), _cls(270)) == []


def test_over_power_engine_violates_with_buffer():
    # 240hp build in the 140 class: 238.6 > 140 + 5.
    violations = evaluate_tt_parts(_parts(), _cls(140))
    assert any("exceeds the 140hp class" in v for v in violations)


def test_buffer_tolerates_borderline_power():
    # 238.6hp in a 240 class: within +HP_BUFFER -> no power violation.
    violations = evaluate_tt_parts(_parts(), _cls(240))
    assert not any("exceeds" in v for v in violations)


def test_unknown_engine_power_is_explicit_violation():
    # An unmodelled engine must NOT pass silently.
    violations = evaluate_tt_parts(
        _parts(engine="Definitely_Not_Modelled_900HP"), _cls(480)
    )
    assert any("could not be verified" in v for v in violations)


def test_mod_tire_is_violation():
    # More Tuning Baja tire (known_mod_parts registry, not a vanilla key) on
    # a tire slot. The stock-part registry normally loads from the game
    # database (absent in CI/test envs) — stub it so the test is
    # environment-independent.
    with patch("amc.mod_detection.get_stock_part_keys", return_value={"201"}):
        violations = evaluate_tt_parts(
            _parts(extra=[{"Slot": 19, "Key": "HeavyDutyBaja60FrontTire"}]), _cls(480)
        )
    assert any("Non-vanilla tires" in v for v in violations)


def test_vanilla_tires_pass():
    with patch("amc.mod_detection.get_stock_part_keys", return_value={"201"}):
        assert evaluate_tt_parts(_parts(extra=[{"Slot": 19, "Key": "201"}]), _cls(480)) == []


def test_default_vehicle_types_are_small_pickup():
    assert _cls(140).allowed_vehicle_types == ["Small", "Pickup"]


def test_buffer_constant_is_five():
    assert HP_BUFFER == 5
