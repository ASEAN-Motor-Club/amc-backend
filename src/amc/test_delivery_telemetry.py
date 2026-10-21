"""Tests for delivery telemetry: snapshot ingest, row enrichment, and the
unknown-cargo marker (behavior-neutral, fail-safe by design)."""

from unittest.mock import MagicMock

from django.test import TestCase

from amc import delivery_telemetry
from amc.handlers.cargo import process_cargo_log
from amc.models import Player


def _snapshot_payload(delivery_id=42, cargo_key="Moonshine", num_cargos=5):
    return {
        "data": [
            {
                "DeliveryPointGuid": "sender-guid",
                "PaymentMultiplier": 2.5,
                "BasePayment": 100,
                "DemandConfigs": [],
                "Net_Deliveries": [],
            },
            {
                "DeliveryPointGuid": "receiver-guid",
                "PaymentMultiplier": 5.0,
                "BasePayment": 200,
                "DemandConfigs": [{"CargoKey": cargo_key, "PaymentMultiplier": 1.5}],
                "Net_Deliveries": [
                    {
                        "ID": delivery_id,
                        "CargoKey": cargo_key,
                        "NumCargos": num_cargos,
                        "Weight": 94.2,
                        "PaymentMultiplierByDemand": 1.3,
                        "PaymentMultiplierBySupply": 0.9,
                        "PaymentMultiplierByBalanceConfig": 1.0,
                        "PathDistance": 612345.0,
                        "PathClimbHeight": 120.0,
                        "PathSpeedKPH": 45.0,
                        "TimerSeconds": 0,
                        "DeliveryFlags": 0,
                        "SenderPoint": "sender-guid",
                        "ReceiverPoint": "receiver-guid",
                        "RegisteredTimeSeconds": 123.0,
                    }
                ],
            },
        ]
    }


class SnapshotIngestTests(TestCase):
    def setUp(self):
        delivery_telemetry._snapshot_deliveries = {}
        delivery_telemetry._snapshot_points = {}
        delivery_telemetry._snapshot_fetched_at = None

    def tearDown(self):
        delivery_telemetry._snapshot_deliveries = {}
        delivery_telemetry._snapshot_points = {}
        delivery_telemetry._snapshot_fetched_at = None

    def test_apply_snapshot_indexes_deliveries_and_points(self):
        points, deliveries = delivery_telemetry.apply_snapshot(_snapshot_payload())
        self.assertEqual((points, deliveries), (2, 1))
        entry = delivery_telemetry.telemetry_for_delivery(42)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["cargo_key"], "Moonshine")
        self.assertEqual(entry["num_cargos"], 5)
        self.assertEqual(entry["payment_multiplier_by_demand"], 1.3)
        self.assertEqual(entry["receiver_payment_multiplier"], 5.0)
        self.assertEqual(entry["receiver_demand_configs"][0]["CargoKey"], "Moonshine")

    def test_no_snapshot_returns_none(self):
        self.assertIsNone(delivery_telemetry.telemetry_for_delivery(42))

    def test_zero_and_negative_ids_ignored(self):
        payload = _snapshot_payload(delivery_id=42)
        payload["data"][1]["Net_Deliveries"].append({"ID": 0, "CargoKey": "X"})
        payload["data"][1]["Net_Deliveries"].append({"ID": -1, "CargoKey": "Y"})
        _, deliveries = delivery_telemetry.apply_snapshot(payload)
        self.assertEqual(deliveries, 1)  # only ID=42 counts

    def test_vehicle_cargo_entry_indexed(self):
        vehicle = {
            "delivery_id": 777,
            "cargo_key": "Moonshine",
            "weight": 95.29,
            "game_payment_base": 42305,
            "game_payment_shadowed": 479007,
            "source": "vehicle_cargos",
        }
        delivery_telemetry.apply_snapshot(_snapshot_payload(), {777: vehicle})
        entry = delivery_telemetry.telemetry_for_delivery(777)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["cargo_key"], "Moonshine")
        self.assertEqual(entry["game_payment_base"], 42305)
        self.assertEqual(entry["game_payment_shadowed"], 479007)
        self.assertEqual(entry["source"], "vehicle_cargos")

    def test_vehicle_cargo_entry_rejects_bad_ids(self):
        self.assertIsNone(delivery_telemetry._vehicle_cargo_entry({"Net_DeliveryId": 0}))
        self.assertIsNone(delivery_telemetry._vehicle_cargo_entry(None))
        self.assertIsNone(delivery_telemetry._vehicle_cargo_entry({"Net_DeliveryId": "x"}))

    def test_vehicle_fetch_soft_fail_keeps_board_snapshot(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status = 503

        async def text():
            return "busy"

        resp.text = text

        async def get(*args, **kwargs):
            return resp

        session.get = get
        import asyncio

        # Board fetch fails (any exception) — the cron-level caller handles
        # it; the point is that this must not corrupt the snapshot state.
        asyncio.run(delivery_telemetry.refresh_background(session))
        delivery_telemetry.apply_snapshot(_snapshot_payload(), None)
        self.assertIsNotNone(delivery_telemetry.telemetry_for_delivery(42))

    def test_refresh_snapshot_soft_failure(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status = 503

        async def text():
            return "busy"

        resp.text = text

        async def get(*args, **kwargs):
            return resp

        session.get = get

        import asyncio

        asyncio.run(delivery_telemetry.refresh_background(session))


class GamedataAlertTests(TestCase):
    """Alert plumbing: temp channel default + silent no-op paths."""

    def test_temp_channel_default(self):
        # Yuuka, 2026-10-20: namer-review channel as temporary home.
        from django.conf import settings

        self.assertEqual(
            settings.DISCORD_GAMEDATA_ALERT_CHANNEL_ID, 1366478091131551834
        )

    def test_no_client_is_noop(self):
        from amc.pipeline.discord import post_discord_gamedata_alert

        post_discord_gamedata_alert(
            None,
            kind="unknown_cargo",
            title="t",
            description="d",
        )  # must not raise

    def test_missing_channel_is_noop(self):
        from unittest.mock import MagicMock

        from amc.pipeline.discord import post_discord_gamedata_alert

        client = MagicMock()
        client.get_channel.return_value = None
        post_discord_gamedata_alert(
            client,
            kind="unknown_cargo",
            title="t",
            description="d",
        )
        client.get_channel.assert_called_once()


class CargoRowEnrichmentTests(TestCase):
    def setUp(self):
        delivery_telemetry.apply_snapshot(_snapshot_payload(delivery_id=77))
        self.player = Player.objects.create(unique_id=76561190000000001)

    def tearDown(self):
        delivery_telemetry._snapshot_deliveries = {}
        delivery_telemetry._snapshot_points = {}
        delivery_telemetry._snapshot_fetched_at = None

    def _cargo(self, cargo_key, delivery_id):
        return {
            "Net_CargoKey": cargo_key,
            "Net_Payment": 12720,
            "Net_Weight": 94.2,
            "Net_Damage": 0.0,
            "Net_DeliveryId": delivery_id,
            "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
            "Net_DestinationLocation": {"X": 100_000, "Y": 0, "Z": 0},
        }

    async def test_row_enriched_with_telemetry(self):
        log = await process_cargo_log(
            self._cargo("Moonshine", 77), self.player, None, None
        )
        telemetry = log.data.get("amc_delivery_telemetry")
        self.assertIsNotNone(telemetry)
        self.assertEqual(telemetry["num_cargos"], 5)
        self.assertEqual(telemetry["payment_multiplier_by_demand"], 1.3)
        self.assertNotIn("amc_unknown_cargo", log.data)

    async def test_cache_miss_leaves_row_unchanged(self):
        log = await process_cargo_log(
            self._cargo("Moonshine", 999), self.player, None, None
        )
        self.assertNotIn("amc_delivery_telemetry", log.data)
        self.assertNotIn("amc_unknown_cargo", log.data)

    async def test_unknown_cargo_marked(self):
        log = await process_cargo_log(
            self._cargo("TotallyNewCargo_999", 0), self.player, None, None
        )
        self.assertIs(log.data.get("amc_unknown_cargo"), True)
        # Known cargo stays unmarked even with a zero delivery id.
        log2 = await process_cargo_log(
            self._cargo("Moonshine", 0), self.player, None, None
        )
        self.assertNotIn("amc_unknown_cargo", log2.data)

    async def test_known_cargo_unmarked(self):
        log = await process_cargo_log(
            self._cargo("CocaineBricks", 77), self.player, None, None
        )
        self.assertNotIn("amc_unknown_cargo", log.data)
        self.assertIsNotNone(log.data.get("amc_delivery_telemetry"))