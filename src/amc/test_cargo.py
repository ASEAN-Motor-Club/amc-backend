from django.test import TestCase
from amc.cargo import get_cargo_bonus
from amc.handlers.cargo import _parse_cargos


def _make_event(*cargos):
    """Helper: wrap a list of cargo dicts in a minimal ServerCargoArrived event."""
    return {"data": {"Cargos": list(cargos)}}


def _cargo(key="Wood", payment=1000, delivery_id=None):
    """Build a minimal cargo dict."""
    c = {"Net_CargoKey": key, "Net_Payment": payment, "Net_Damage": 0.0}
    if delivery_id is not None:
        c["Net_DeliveryId"] = delivery_id
    return c


class ParseCargosTests(TestCase):
    def test_normal_cargo_included(self):
        """Cargos with a real DeliveryId pass through unchanged."""
        event = _make_event(_cargo("Wood", 1000, delivery_id=42))
        result = _parse_cargos(event)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["Net_CargoKey"], "Wood")

    def test_zero_delivery_id_included(self):
        """Cargos with DeliveryId == 0 are now included — fraud detection still applies."""
        event = _make_event(_cargo("Wood", 5000, delivery_id=0))
        result = _parse_cargos(event)
        self.assertEqual(len(result), 1, "DeliveryId=0 cargo should be processed, not dropped")

    def test_mixed_cargos_all_returned(self):
        """Both DeliveryId=0 and real DeliveryId cargos are returned."""
        event = _make_event(
            _cargo("Coal", 2000, delivery_id=0),
            _cargo("Iron", 3000, delivery_id=99),
        )
        result = _parse_cargos(event)
        self.assertEqual(len(result), 2)
        keys = [c["Net_CargoKey"] for c in result]
        self.assertIn("Coal", keys)
        self.assertIn("Iron", keys)

    def test_cargo_without_delivery_id_field_included(self):
        """Cargos that have no Net_DeliveryId key at all are treated as normal."""
        event = _make_event(_cargo("Stone", 800))  # no delivery_id kwarg → key absent
        result = _parse_cargos(event)
        self.assertEqual(len(result), 1)

    def test_negative_payment_raises(self):
        """Negative payment is always a hard error regardless of DeliveryId."""
        event = _make_event(_cargo("Hack", -500, delivery_id=1))
        with self.assertRaises(ValueError):
            _parse_cargos(event)

    def test_multiple_zero_delivery_ids_all_included(self):
        """Multiple DeliveryId=0 cargos are all kept and processed."""
        event = _make_event(
            _cargo("A", 100, delivery_id=0),
            _cargo("B", 200, delivery_id=0),
        )
        result = _parse_cargos(event)
        self.assertEqual(len(result), 2)


class GetCargoBonusTests(TestCase):
    def test_oak_log_zero_damage(self):
        # 0% damage → full bonus (100% of payment)
        self.assertEqual(get_cargo_bonus("Log_Oak_12ft", 8641, 0.0), 8641)

    def test_oak_log_full_damage(self):
        # 100% damage → no bonus
        self.assertEqual(get_cargo_bonus("Log_Oak_12ft", 8641, 1.0), 0)

    def test_oak_log_partial_damage(self):
        # 50% damage → 50% bonus
        self.assertEqual(get_cargo_bonus("Log_Oak_12ft", 10000, 0.5), 5000)

    def test_oak_log_quarter_damage(self):
        # 25% damage → 75% bonus
        self.assertEqual(get_cargo_bonus("Log_Oak_12ft", 10000, 0.25), 7500)

    def test_unknown_cargo_no_bonus(self):
        self.assertEqual(get_cargo_bonus("oranges", 10000, 0.0), 0)

    def test_unknown_cargo_with_damage(self):
        self.assertEqual(get_cargo_bonus("apples", 10000, 0.5), 0)
