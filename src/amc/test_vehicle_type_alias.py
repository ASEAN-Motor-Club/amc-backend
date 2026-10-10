"""vehicle_type_for must resolve vehicles whose DataTable RowName is numeric.

The gamedata `vehicles` table is keyed by RowName; Hana/Stinger/Maity/Spider
are rows '1'..'4' while the runtime vehicle fullName carries the blueprint
asset name ("Hana_C Default__Hana"). Before the blueprint_path alias,
vehicle_type_for() returned None for them and TT DQ rejected them with
"Vehicle type could not be verified" (FreyFrey's Hana, 2026-10-10).
"""

import sqlite3

import pytest

from amc import mod_detection


def _make_db(
    path,
    columns="id TEXT, name TEXT, vehicle_type TEXT, truck_class TEXT, blueprint_path TEXT",
):
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE vehicles ({columns})")
    con.commit()
    con.close()


@pytest.fixture
def gamedata_db(tmp_path, monkeypatch):
    db = tmp_path / "gamedata.db"
    _make_db(db)
    con = sqlite3.connect(db)
    rows = [
        ("1", "1_VehicleName", "Pickup", "LightDuty", "Hana_C"),
        ("2", "2_VehicleName", "Small", "None", "Stinger_C"),
        ("Bora", "Bora_VehicleName", "Pickup", "LightDuty", "Bora_C"),
    ]
    con.executemany("INSERT INTO vehicles VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()
    monkeypatch.setattr(mod_detection, "GAME_DB_PATH", str(db))
    monkeypatch.setattr(mod_detection, "_vehicle_type_map", None)
    monkeypatch.setattr(mod_detection, "_part_compatible_types", None)
    yield db
    monkeypatch.setattr(mod_detection, "_vehicle_type_map", None)
    monkeypatch.setattr(mod_detection, "_part_compatible_types", None)


def test_numeric_rowname_resolves_by_blueprint_asset(gamedata_db):
    assert mod_detection.vehicle_type_for("Hana_C Default__Hana") == "Pickup"
    assert mod_detection.vehicle_type_for("Stinger_C Default__Stinger") == "Small"
    # Regular RowName-keyed vehicle still resolves.
    assert mod_detection.vehicle_type_for("Bora_C Default__Bora") == "Pickup"


def test_numeric_rowname_resolves_by_bare_blueprint(gamedata_db):
    # fullName without the _C suffix still maps via the alias.
    assert mod_detection.vehicle_type_for("Hana Default__Hana") == "Pickup"


def test_loader_survives_db_without_blueprint_path(tmp_path, monkeypatch):
    db = tmp_path / "gamedata.db"
    _make_db(db, columns="id TEXT, vehicle_type TEXT")
    con = sqlite3.connect(db)
    con.execute("INSERT INTO vehicles VALUES ('Bora', 'Pickup')")
    con.commit()
    con.close()
    monkeypatch.setattr(mod_detection, "GAME_DB_PATH", str(db))
    monkeypatch.setattr(mod_detection, "_vehicle_type_map", None)
    monkeypatch.setattr(mod_detection, "_part_compatible_types", None)
    assert mod_detection.vehicle_type_for("Bora_C Default__Bora") == "Pickup"
    monkeypatch.setattr(mod_detection, "_vehicle_type_map", None)
    monkeypatch.setattr(mod_detection, "_part_compatible_types", None)
