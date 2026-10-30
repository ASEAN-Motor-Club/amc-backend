"""Tests for delivery telemetry: snapshot ingest, row enrichment, and the
unknown-cargo marker (behavior-neutral, fail-safe by design)."""

from unittest.mock import MagicMock

import asyncio
from datetime import timedelta

from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone

from amc import delivery_telemetry
from amc.handlers.cargo import process_cargo_log
from amc.models import Character, DeliveryPoint, DeliveryQuoteCache, Player


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

    def test_vehicle_fetch_retries_after_failure(self):

        calls = {"n": 0, "path": ""}

        class Resp:
            status = 200

            async def json(self, content_type=None):
                if calls["n"] == 0:
                    calls["n"] += 1
                    raise RuntimeError("transient")
                calls["n"] += 1
                if calls["path"] == "players":
                    calls["path"] = "veh"
                    return {"data": [{"CharacterGuid": "ABC"}]}
                return {"data": [{"fullName": "Golima_Semi_C /Game/Maps/Jeju/Jeju_World.Jeju_World:PersistentLevel.Golima_Semi_C_2146948537", "cargoSpaces": [{"cargos": [
                    {
                        "Net_CargoKey": "Milk",
                        "Net_DeliveryId": 555,
                        "Net_Weight": 1030.0,
                        "Net_Payment": {"BaseValue": 662},
                    }
                ]}]}]}

        def get(*args, **kwargs):
            resp = Resp()
            calls["path"] = "players" if str(args[0]).endswith("/players") else "veh"

            class Ctx:
                async def __aenter__(self):
                    return resp

                async def __aexit__(self, *exc):
                    return False

            return Ctx()

        session = MagicMock()
        session.get = get
        got = asyncio.run(delivery_telemetry.fetch_vehicle_cargos(session))
        self.assertIn(555, got)
        self.assertEqual(got[555][0]["game_payment_base"], 662)
        self.assertEqual(got[555][0]["vehicle"], "Golima_Semi_C")

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
        # The snapshot entry for id 77 belongs to Moonshine: a different cargo
        # must never inherit another cargo's telemetry/quote.
        self.assertIsNone(log.data.get("amc_delivery_telemetry"))

class MultiStackQuoteTests(TestCase):
    """A delivery id can cover several batches of one cargo.

    Two batches sharing an id were registered at different pickup refreshes
    and carry different prices; the snapshot must keep both and enforce only
    above the highest (2026-10-22, Bitzer coal: 17 units paid 1,920 were
    clawed against the other batch's 1,523 quote).
    """

    def setUp(self):
        delivery_telemetry._snapshot_deliveries = {}
        delivery_telemetry._snapshot_points = {}
        delivery_telemetry._snapshot_fetched_at = None

    def tearDown(self):
        delivery_telemetry._snapshot_deliveries = {}
        delivery_telemetry._snapshot_points = {}
        delivery_telemetry._snapshot_fetched_at = None

    @staticmethod
    def _entry(delivery_id, cargo_key, weight, base, vehicle):
        return {
            "delivery_id": delivery_id,
            "cargo_key": cargo_key,
            "vehicle": vehicle,
            "weight": weight,
            "game_payment_base": base,
            "game_payment_shadowed": base * 10,
            "source": "vehicle_cargos",
        }

    def _ingest(self, entries):
        grouped = {}
        for entry in entries:
            grouped.setdefault(entry["delivery_id"], []).append(entry)
        delivery_telemetry.apply_snapshot({"data": []}, grouped)

    def test_both_batches_are_kept(self):
        self._ingest(
            [
                self._entry(168967, "Coal", 600.0, 1920, "Oldum_C"),
                self._entry(168967, "Coal", 600.0, 1523, "Taber_Rear_C"),
            ]
        )
        telemetry = delivery_telemetry.telemetry_for_delivery(
            168967, cargo_key="Coal", weight=600.0
        )
        self.assertIsNotNone(telemetry)
        self.assertEqual(telemetry["game_payment_bases"], [1523, 1920])
        self.assertTrue(telemetry["multi_quote"])
        # enforcement value is the HIGHEST registered quote
        self.assertEqual(telemetry["game_payment_base"], 1920)

    def test_single_quote_payload_unchanged(self):
        self._ingest([self._entry(42, "Coal", 600.0, 1523, "Oldum_C")])
        telemetry = delivery_telemetry.telemetry_for_delivery(
            42, cargo_key="Coal", weight=600.0
        )
        self.assertEqual(telemetry["game_payment_base"], 1523)
        self.assertNotIn("multi_quote", telemetry)
        self.assertNotIn("game_payment_bases", telemetry)

    def test_different_cargo_never_shares_a_quote(self):
        """Corn paying 3x pumpkin must not excuse a pumpkin payout."""
        self._ingest(
            [
                self._entry(77, "CornPallet", 174.0, 30000, "Oldum_C"),
                self._entry(77, "PumpkinPallet", 174.0, 10000, "Oldum_C"),
            ]
        )
        corn = delivery_telemetry.telemetry_for_delivery(
            77, cargo_key="CornPallet", weight=174.0
        )
        pumpkin = delivery_telemetry.telemetry_for_delivery(
            77, cargo_key="PumpkinPallet", weight=174.0
        )
        self.assertEqual(corn["game_payment_base"], 30000)
        self.assertEqual(pumpkin["game_payment_base"], 10000)
        self.assertNotIn("multi_quote", pumpkin)

    def test_unknown_cargo_returns_none(self):
        self._ingest([self._entry(77, "CornPallet", 174.0, 30000, "Oldum_C")])
        self.assertIsNone(
            delivery_telemetry.telemetry_for_delivery(
                77, cargo_key="PumpkinPallet", weight=174.0
            )
        )

    def test_weight_mismatch_falls_back_to_cargo_matches(self):
        """Per-unit weight variance (containers) must not lose coverage."""
        self._ingest([self._entry(9, "SteelCoil_10t", 14000.0, 5000, "Oldum_C")])
        telemetry = delivery_telemetry.telemetry_for_delivery(
            9, cargo_key="SteelCoil_10t", weight=13999.2
        )
        self.assertIsNotNone(telemetry)
        self.assertEqual(telemetry["game_payment_base"], 5000)

    def test_weight_scoping_never_lowers_the_enforcement_quote(self):
        """Weight picks the reported stack, not the ceiling.

        The same delivery id can be registered per unit and again as one
        aggregate entry; the two carry different prices and the arrival's
        weight matches only one of them.
        """
        self._ingest(
            [
                self._entry(11, "IronOre", 2000.0, 4000, "Atlas_8x4_Dump_C"),
                self._entry(11, "IronOre", 3000.0, 5500, "Oldum_C"),
            ]
        )
        light = delivery_telemetry.telemetry_for_delivery(
            11, cargo_key="IronOre", weight=2000.0
        )
        # enforcement spans the delivery id — highest quote wins
        self.assertEqual(light["game_payment_base"], 5500)
        self.assertEqual(light["weight_scoped_base"], 4000)
        self.assertEqual(light["game_payment_bases"], [4000, 5500])
        # ...but the row still reports the stack that actually arrived
        self.assertEqual(light["vehicle"], "Atlas_8x4_Dump_C")

    def test_aggregate_sibling_registration_sets_the_ceiling(self):
        """2026-10-30 JunWu: delivery 151426 was registered twice — 5,000 kg
        per unit at 1,478 and one 21,803 kg aggregate at 3,301.  The arrival
        weighed 5,000, matched the cheap entry, and a legitimate 3,301/unit
        payout was clawed 1,823/unit (7,292 over four units)."""
        self._ingest(
            [
                self._entry(
                    151426, "WoodPlank_14ft_5t", 5000.00048828125, 1478, "Flanker3S_C"
                ),
                self._entry(
                    151426, "WoodPlank_14ft_5t", 21803.841796875, 3301, "Olbe_C"
                ),
            ]
        )
        telemetry = delivery_telemetry.telemetry_for_delivery(
            151426, cargo_key="WoodPlank_14ft_5t", weight=5000.00048828125
        )
        self.assertEqual(telemetry["game_payment_base"], 3301)
        self.assertEqual(telemetry["weight_scoped_base"], 1478)
        self.assertEqual(telemetry["vehicle"], "Flanker3S_C")
        self.assertTrue(telemetry["multi_quote"])

    def test_stale_snapshot_returns_none(self):
        self._ingest([self._entry(42, "Coal", 600.0, 1523, "Oldum_C")])
        delivery_telemetry._snapshot_fetched_at -= (
            delivery_telemetry.SNAPSHOT_MAX_AGE_SECONDS + 1
        )
        self.assertIsNone(
            delivery_telemetry.telemetry_for_delivery(
                42, cargo_key="Coal", weight=600.0
            )
        )


class QuoteCacheTests(TestCase):
    """Reconnect recovery: an arrival that lost its delivery id.

    The game resets Net_DeliveryId to 0 when a player disconnects mid-haul.
    Every tick we cache the registrations seen on players' vehicles; an
    unregistered arrival must be matched back to its own registration by
    character + cargo + weight + route, and must never borrow another
    character's price (single-player cargo has no cached row at all).
    """

    SENDER = (0.0, 0.0, 0.0)
    RECEIVER = (100_000.0, 0.0, 0.0)

    def setUp(self):
        self.player = Player.objects.create(unique_id=76561190000000002)
        self.character = Character.objects.create(
            player=self.player, guid="GUID0001", name="Hauler"
        )
        self.other = Character.objects.create(
            player=self.player, guid="GUID0002", name="Stranger"
        )
        self.sender = DeliveryPoint.objects.create(
            guid="sender-1", name="Pickup", coord=Point(*self.SENDER, srid=3857)
        )
        self.receiver = DeliveryPoint.objects.create(
            guid="receiver-1", name="Drop", coord=Point(*self.RECEIVER, srid=3857)
        )

    def _entry(self, **overrides):
        entry = {
            "delivery_id": 555,
            "cargo_key": "Fuel",
            "vehicle": "Taber_Lead_C",
            "weight": 800.0,
            "game_payment_base": 152,
            "sender_location": dict(zip(("X", "Y", "Z"), self.SENDER)),
            "destination_location": dict(zip(("X", "Y", "Z"), self.RECEIVER)),
            "character_guid": "GUID0001",
            "source": "vehicle_cargos",
        }
        entry.update(overrides)
        return entry

    def _write(self, **entry_overrides):
        return delivery_telemetry.record_quote_cache(
            {555: [self._entry(**entry_overrides)]}
        )

    def _lookup(self, **overrides):
        kwargs = {
            "character_id": self.character.id,
            "cargo_key": "Fuel",
            "weight": 800.0,
            "sender_point": self.sender,
            "destination_point": self.receiver,
        }
        kwargs.update(overrides)
        return delivery_telemetry.lookup_quote_cache(**kwargs)

    def test_reconnect_arrival_matched_to_cached_quote(self):
        self.assertEqual(self._write(), 1)
        telemetry = self._lookup()
        self.assertIsNotNone(telemetry)
        self.assertEqual(telemetry["game_payment_base"], 152)
        self.assertEqual(telemetry["source"], "quote_cache")
        self.assertEqual(telemetry["delivery_id"], 555)

    def test_unregistered_cargo_never_matches_another_character(self):
        self._write()
        # Single-player cargo: no cached registration for this character.
        self.assertIsNone(self._lookup(character_id=self.other.id))

    def test_weight_mismatch_rejected(self):
        self._write()
        self.assertIsNone(self._lookup(weight=400.0))

    def test_cargo_mismatch_rejected(self):
        self._write()
        self.assertIsNone(self._lookup(cargo_key="Coal"))

    def test_route_mismatch_rejected(self):
        self._write()
        elsewhere = DeliveryPoint.objects.create(
            guid="sender-2", name="Other pickup", coord=Point(0, 900_000, 0, srid=3857)
        )
        self.assertIsNone(self._lookup(sender_point=elsewhere))

    def test_stale_registration_ignored(self):
        self._write()
        DeliveryQuoteCache.objects.update(
            last_seen=timezone.now() - timedelta(hours=13)
        )
        self.assertIsNone(self._lookup())

    def test_upsert_refreshes_quote_and_last_seen(self):
        self._write()
        DeliveryQuoteCache.objects.update(
            last_seen=timezone.now() - timedelta(hours=5)
        )
        self._write(game_payment_base=180)
        self.assertEqual(DeliveryQuoteCache.objects.count(), 1)
        row = DeliveryQuoteCache.objects.get()
        self.assertEqual(row.base_value, 180)
        self.assertLess(
            (timezone.now() - row.last_seen).total_seconds(), 60
        )

    def test_highest_cached_quote_wins(self):
        delivery_telemetry.record_quote_cache(
            {
                555: [self._entry(game_payment_base=152)],
                556: [self._entry(delivery_id=556, game_payment_base=180)],
            }
        )
        telemetry = self._lookup()
        self.assertEqual(telemetry["game_payment_base"], 180)
        self.assertEqual(telemetry["delivery_id"], 556)

    def test_nonzero_arrival_only_sees_its_own_registration(self):
        """A delivery that kept its id must never borrow another haul's quote.

        2026-10-26 FreyFrey: arrival 165942 was validated against 166310's
        registration (a heavier bag on another destination) and clawed 28
        units of a legitimate payout.
        """
        delivery_telemetry.record_quote_cache(
            {
                555: [self._entry(game_payment_base=152)],
                556: [self._entry(delivery_id=556, game_payment_base=180)],
            }
        )
        telemetry = self._lookup(arrival_delivery_id=556)
        self.assertIsNotNone(telemetry)
        self.assertEqual(telemetry["delivery_id"], 556)
        self.assertEqual(telemetry["game_payment_base"], 180)

    def test_nonzero_arrival_without_its_own_registration_gets_nothing(self):
        """No quote for this delivery => fall through to history, never
        borrow a same-shape quote the way the heuristic does."""
        self._write()  # only 555 is cached
        self.assertIsNone(self._lookup(arrival_delivery_id=999))

    def test_idless_arrival_keeps_the_heuristic_match(self):
        """The reconnect case this cache exists for still works."""
        self._write()
        telemetry = self._lookup(arrival_delivery_id=0)
        self.assertIsNotNone(telemetry)
        self.assertEqual(telemetry["delivery_id"], 555)

    def test_duplicate_units_collapse_to_one_row(self):
        # A vehicle hauling N units of one cargo reports N entries with the
        # same (character, delivery_id, cargo, weight) key.  Postgres rejects
        # an ON CONFLICT DO UPDATE touching one row twice in one statement,
        # so they must be collapsed before the insert (prod bug 2026-10-24:
        # 100-unit Milk haul made every cache write fail silently).
        entries = [self._entry(game_payment_base=300) for _ in range(100)]
        entries.append(self._entry(game_payment_base=350))
        written = delivery_telemetry.record_quote_cache({555: entries})
        self.assertEqual(written, 1)
        self.assertEqual(DeliveryQuoteCache.objects.count(), 1)
        self.assertEqual(DeliveryQuoteCache.objects.get().base_value, 350)
