"""Tests for amc/vehicle_weight.py — weight + PWR lines for /check_parts
and the parts audit.

The gamedata loaders are pointed at a tiny fixture sqlite DB (same table
shape as the pak-derived gamedata snapshot: ``vehicle_weights`` +
``vehicle_parts.mass_kg``); engine masses come from the committed powercalc
snapshot. Every branch of the explicit-degradation rendering is asserted —
no fabricated totals anywhere.
"""

import sqlite3

import pytest

from amc import vehicle_weight as vw
from amc.parts_audit import summarize_parts
from powercalc.vehicle_setup import compute_peak_hp


def _make_db(tmp_path):
    path = tmp_path / "gamedata.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE vehicle_weights (
            vehicle_id INTEGER, chassis_mass_kg REAL, blueprint_path TEXT);
        CREATE TABLE vehicle_parts (
            name TEXT, part_type TEXT, mass_kg REAL);
        """
    )
    conn.executemany(
        "INSERT INTO vehicle_weights VALUES (?,?,?)",
        [
            (1, 1420.0, "Default__Elisa2_C"),
            (2, 1050.0, "Miata_C"),  # bare-shape blueprint_path
            (3, None, "Default__Barebones_C"),  # NULL mass excluded by query
        ],
    )
    conn.executemany(
        "INSERT INTO vehicle_parts VALUES (?,?,?)",
        [
            ("201", "Intake", 12.0),
            ("Damper200", "Suspension_Damper", 8.0),
            ("WheelSpacer50", "WheelSpacer", 1.2),
            ("Turbocharger_Stage1", "Turbocharger", 15.0),
        ],
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def weight_db(tmp_path, monkeypatch):
    """Point the module at the fixture DB and clear its module caches."""
    path = _make_db(tmp_path)
    monkeypatch.setattr(vw, "GAME_DB_PATH", str(path))
    vw.reset_caches()
    yield path
    vw.reset_caches()


# Golden values (printed once from the committed snapshot, 2026-09-10):
# SmallBlock_240HP + intake 201 + Turbocharger_Stage1 -> 293.2352 hp
GOLDEN_PEAK = 293.2352326527303


def _golden_parts():
    return [
        {"Key": "SmallBlock_240HP", "Slot": 2},
        {"Key": "201", "Slot": 5},
        {"Key": "Turbocharger_Stage1", "Slot": 7},
    ]


class TestBlueprintKey:
    def test_full_name_first_token(self):
        assert vw.blueprint_key("Elisa2_C Default__Elisa2") == "Elisa2"

    def test_bare_class_suffix(self):
        assert vw.blueprint_key("Miata_C Default__Miata") == "Miata"

    def test_no_suffix(self):
        assert vw.blueprint_key("Van Default__Van") == "Van"

    def test_empty(self):
        assert vw.blueprint_key("") == ""


class TestChassisMass:
    def test_default__shape(self, weight_db):
        assert vw.chassis_mass("Elisa2_C Default__Elisa2") == 1420.0

    def test_bare_shape(self, weight_db):
        assert vw.chassis_mass("Miata_C Default__Miata") == 1050.0

    def test_unknown_vehicle(self, weight_db):
        assert vw.chassis_mass("Taxi_C Default__Taxi") is None

    def test_null_mass_row_ignored(self, weight_db):
        assert vw.chassis_mass("Barebones_C Default__Barebones") is None

    def test_case_insensitive(self, weight_db):
        assert vw.chassis_mass("ELISA2_C Default__ELISA2") == 1420.0


class TestPartMass:
    def test_exact_row(self, weight_db):
        assert vw.part_mass("201") == 12.0

    def test_tuning_family_base_fallback(self, weight_db):
        # Damper200_200 -> base row Damper200 (Suspension_Damper is a family)
        assert vw.part_mass("Damper200_200") == 8.0
        assert vw.part_mass("WheelSpacer50_30") == 1.2

    def test_no_blind_strip_for_non_families(self, weight_db):
        # tire-style "201_50" must NOT resolve onto intake "201" — Intake is
        # not in the tuning families; only slot 2 may fall through to engines.
        assert vw.part_mass("201_50") is None

    def test_engine_slot_uses_powercalc_snapshot(self, weight_db):
        assert vw.part_mass("SmallBlock_240HP", slot=2) == 250.0

    def test_engine_key_ignored_outside_engine_slot(self, weight_db):
        # engine rows are not VehicleParts rows; without slot=2 no engine probe
        assert vw.part_mass("SmallBlock_240HP") is None

    def test_ev_motor_has_no_mass(self, weight_db):
        assert vw.part_mass("Electric_130HP", slot=2) is None

    def test_unknown_key(self, weight_db):
        assert vw.part_mass("ModPart_Whatever") is None
        assert vw.part_mass(None) is None


class TestComputeWeightSummary:
    def test_full_known_setup(self, weight_db):
        parts = _golden_parts() + [
            {"Key": "Damper200_200", "Slot": 9},
            {"Key": "201_50", "Slot": 19},  # uncounted (blind-strip guard)
            {"Key": "ModPart_Whatever", "Slot": 21},  # uncounted (mod part)
        ]
        s = vw.compute_weight_summary("Elisa2_C Default__Elisa2", parts)
        assert s.chassis_kg == 1420.0
        # 250 engine + 12 intake + 15 turbo + 8 damper
        assert s.parts_kg == 285.0
        assert s.resolved == 4
        assert s.uncounted == 2
        assert s.total_kg == 1705.0

    def test_unknown_chassis_keeps_parts_lower_bound(self, weight_db):
        parts = [{"Key": "201", "Slot": 5}]
        s = vw.compute_weight_summary("Taxi_C Default__Taxi", parts)
        assert s.chassis_kg is None
        assert s.parts_kg == 12.0
        assert s.total_kg is None

    def test_empty_parts(self, weight_db):
        s = vw.compute_weight_summary("Elisa2_C Default__Elisa2", [])
        assert s.parts_kg == 0.0
        assert s.resolved == 0
        assert s.uncounted == 0
        assert s.total_kg == 1420.0


class TestFormatWeightLines:
    def test_known_total(self, weight_db):
        parts = _golden_parts() + [{"Key": "Damper200_200", "Slot": 9}]
        s = vw.compute_weight_summary("Elisa2_C Default__Elisa2", parts)
        lines = vw.format_weight_lines(s, None)
        assert lines == ["Weight: 1,705 kg (1,420 chassis + 285 parts)"]

    def test_uncounted_called_out(self, weight_db):
        parts = _golden_parts() + [{"Key": "ModPart_Whatever", "Slot": 21}]
        s = vw.compute_weight_summary("Elisa2_C Default__Elisa2", parts)
        lines = vw.format_weight_lines(s, None)
        assert lines == ["Weight: 1,697 kg (1,420 chassis + 277 parts, 1 uncounted)"]

    def test_unknown_chassis_with_parts(self, weight_db):
        s = vw.compute_weight_summary(
            "Taxi_C Default__Taxi", [{"Key": "201", "Slot": 5}]
        )
        assert vw.format_weight_lines(s, None) == [
            "Weight: Unknown chassis + 12 kg parts"
        ]

    def test_no_data_at_all(self, weight_db):
        s = vw.compute_weight_summary("Taxi_C Default__Taxi", [])
        assert vw.format_weight_lines(s, None) == ["Weight: Unknown"]

    def test_pwr_only_when_total_and_peak(self, weight_db):
        parts = _golden_parts() + [{"Key": "Damper200_200", "Slot": 9}]
        s = vw.compute_weight_summary("Elisa2_C Default__Elisa2", parts)
        # 293.2352 hp / 1.705 t = 172.0 -> "172"
        lines = vw.format_weight_lines(s, GOLDEN_PEAK)
        assert lines == [
            "Weight: 1,705 kg (1,420 chassis + 285 parts)",
            "PWR: 172 hp/t",
        ]

    def test_no_pwr_without_peak(self, weight_db):
        s = vw.compute_weight_summary("Elisa2_C Default__Elisa2", _golden_parts())
        assert vw.format_weight_lines(s, None) == [
            "Weight: 1,697 kg (1,420 chassis + 277 parts)"
        ]


class TestComputePeakHp:
    def test_golden_dyno_build(self):
        assert compute_peak_hp(_golden_parts()) == pytest.approx(
            GOLDEN_PEAK, abs=1e-9
        )

    def test_ev_motor_rating(self):
        # Electric_130HP: MotorMaxPower 1003210.06 -> /10 -> /745.699872
        assert compute_peak_hp([{"Key": "Electric_130HP", "Slot": 2}]) == (
            pytest.approx(134.53268502103217, abs=1e-9)
        )

    def test_unknown_engine(self):
        assert compute_peak_hp([{"Key": "NoSuchEngine", "Slot": 2}]) is None

    def test_no_engine_slot(self):
        assert compute_peak_hp([{"Key": "201", "Slot": 5}]) is None

    def test_teh_pack_engine(self):
        # teh engine pack (PR #120) row, computed through the same path
        assert compute_peak_hp([{"Key": "FerrariV12", "Slot": 2}]) == (
            pytest.approx(799.573381550541, abs=1e-9)
        )


class TestPopupAndAuditWiring:
    def test_weight_popup_lines_one_shot(self, weight_db):
        parts = _golden_parts() + [{"Key": "Damper200_200", "Slot": 9}]
        lines = vw.weight_popup_lines("Elisa2_C Default__Elisa2", parts, GOLDEN_PEAK)
        assert lines == ["Weight: 1,705 kg (1,420 chassis + 285 parts)", "PWR: 172 hp/t"]

    def test_audit_weight_line_joined(self, weight_db):
        parts = _golden_parts() + [{"Key": "Damper200_200", "Slot": 9}]
        line = vw.audit_weight_line("Elisa2_C Default__Elisa2", parts, GOLDEN_PEAK)
        assert line == "Weight: 1,705 kg (1,420 chassis + 285 parts) · PWR: 172 hp/t"

    def test_summarize_parts_inserts_weight_after_power(self, weight_db):
        parts = _golden_parts() + [{"Key": "Damper200_200", "Slot": 9}]
        lines = summarize_parts(parts, "Elisa2_C Default__Elisa2")
        assert lines[0] == "Power: 293.2 hp @ 6,216 rpm · 413.0 Nm @ 4,355 rpm"
        assert lines[1] == "Weight: 1,705 kg (1,420 chassis + 285 parts) · PWR: 172 hp/t"
        assert lines[2:5] == [
            "Engine: SmallBlock_240HP",
            "Intake: 201",
            "Turbocharger: Turbocharger_Stage1",
        ]
        assert lines[5] == "Tires: None"

    def test_summarize_parts_without_vehicle_has_no_weight_line(self, weight_db):
        lines = summarize_parts(_golden_parts())
        assert not any(l.startswith(("Weight", "PWR")) for l in lines)

    def test_summarize_parts_weight_failure_never_breaks_audit(
        self, weight_db, monkeypatch
    ):
        def boom(*a, **k):
            raise RuntimeError("gamedata exploded")

        monkeypatch.setattr(vw, "compute_weight_summary", boom)
        lines = summarize_parts(_golden_parts(), "Elisa2_C Default__Elisa2")
        assert lines[0] == "Power: 293.2 hp @ 6,216 rpm · 413.0 Nm @ 4,355 rpm"
        assert lines[1] == "Engine: SmallBlock_240HP"
        assert not any(l.startswith("Weight") for l in lines)