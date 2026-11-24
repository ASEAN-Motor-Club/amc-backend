"""Tests for the cargo definition rows (Schedule I / vanilla / mod ingest)."""

from django.test import SimpleTestCase

from amc.cargo_definitions import (
    CARGO_ROWS,
    GAME_VERSION,
    MOD_ROWS,
    SCHEDULE_I_ROWS,
    VANILLA_ROWS,
    cargo_is_known,
    get_cargo_row,
)


class CargoDefinitionsTests(SimpleTestCase):
    def test_schedule_i_rows_present(self):
        """The 21 illicit Schedule I cargos (absent from vanilla gamedata)
        are available — Moonshine is the 2026-10-20 claw-forensics anchor."""
        self.assertEqual(len(SCHEDULE_I_ROWS), 21)
        moonshine = get_cargo_row("Moonshine")
        self.assertIsNotNone(moonshine)
        self.assertEqual(moonshine["payment_per_km"], 200)
        self.assertEqual(moonshine["payment_multiplier"], 3.0)
        self.assertEqual(moonshine["base_payment"], 300)
        self.assertEqual(moonshine["weight_min"], 80)
        self.assertEqual(moonshine["weight_max"], 100)

    def test_vanilla_rows_present(self):
        self.assertEqual(len(VANILLA_ROWS), 92)
        kerosene = get_cargo_row("Kerosene")
        self.assertIsNotNone(kerosene)
        self.assertEqual(kerosene["payment_per_km"], 300)
        self.assertEqual(kerosene["payment_multiplier"], 2.0)
        self.assertEqual(kerosene["weight_max"], 800)

    def test_mod_rows_present(self):
        """TurbineEngine comes from Proxys Oversize Cargo V5 (0.7.20): its
        row is identical in the pak's Cargos and Cargos_Proxy tables."""
        self.assertEqual(len(MOD_ROWS), 1)
        row = MOD_ROWS["TurbineEngine"]
        self.assertEqual(get_cargo_row("TurbineEngine"), row)
        self.assertEqual(row["payment_per_km"], 4000)
        self.assertEqual(row["payment_multiplier"], 2.0)
        self.assertEqual(row["base_payment"], 5000)
        self.assertEqual(row["weight_min"], 200000)
        self.assertEqual(row["weight_max"], 200000)
        self.assertEqual(row["num_cargo_min"], 5)
        self.assertEqual(row["num_cargo_max"], 20)

    def test_unknown_cargo_returns_none(self):
        """Fail-safe contract: unknown keys MUST return None (never a row
        with zero/empty bounds) so downstream checks can skip instead of
        assuming pay 0 or infinity."""
        self.assertIsNone(get_cargo_row("NotACargoKey_999"))
        self.assertFalse(cargo_is_known("NotACargoKey_999"))
        self.assertTrue(cargo_is_known("Moonshine"))
        self.assertTrue(cargo_is_known("TurbineEngine"))

    def test_rows_have_required_pricing_fields(self):
        for key, row in CARGO_ROWS.items():
            for field in (
                "payment_per_km",
                "payment_multiplier",
                "base_payment",
                "weight_min",
                "weight_max",
            ):
                self.assertIn(field, row, f"{key} missing {field}")

    def test_game_version_recorded(self):
        self.assertEqual(GAME_VERSION, "v0.7.19")
