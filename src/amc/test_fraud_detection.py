import asyncio
import time
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import sync_to_async
from django.contrib.gis.geos import Point
from django.test import TestCase, override_settings

from amc.factories import CharacterFactory, PlayerFactory
from amc.fraud_detection import (
    CARGO_MAX_ABSOLUTE_PAYMENT,
    CARGO_PER_KM_THRESHOLDS,
    CARGO_PER_UNIT_DEFAULT,
    CARGO_PER_UNIT_THRESHOLDS,
    ROUTE_HISTORY_MIN_SAMPLES,
    PASSENGER_PAYMENT_CEILINGS,
    TOW_PAYMENT_CEILING,
    validate_cargo_payment,
    validate_passenger_payment,
    validate_tow_payment,
)
from amc.models import (
    CharacterLocation,
    Delivery,
    DeliveryPoint,
    ServerCargoArrivedLog,
    ServerPassengerArrivedLog,
    ServerTowRequestArrivedLog,
)
from amc.pipeline.discord import post_discord_fraud_alert
from amc.pipeline.profit import on_player_profit, on_player_profits
from amc.webhook import process_event, process_events

# ---------------------------------------------------------------------------
# Pure function tests — validate_cargo_payment (async)
# ---------------------------------------------------------------------------


class ValidateCargoPaymentTests(TestCase):
    """Tests for validate_cargo_payment."""

    async def test_legitimate_payment_returns_zero(self):
        excess = await validate_cargo_payment(
            cargo_key="BottlePallete",
            payment=5_000,
            quantity=1,
            sender_point=None,
            destination_point=None,
        )
        self.assertEqual(excess, 0)

    async def test_zero_payment_returns_zero(self):
        excess = await validate_cargo_payment(
            cargo_key="BottlePallete",
            payment=0,
            quantity=1,
            sender_point=None,
            destination_point=None,
        )
        self.assertEqual(excess, 0)

    async def test_negative_payment_returns_zero(self):
        excess = await validate_cargo_payment(
            cargo_key="BottlePallete",
            payment=-100,
            quantity=1,
            sender_point=None,
            destination_point=None,
        )
        self.assertEqual(excess, 0)

    async def test_unknown_cargo_uses_default_backstop(self):
        """Unlisted cargo keys fall back to CARGO_PER_UNIT_DEFAULT so a
        game-update cargo is never unprotected."""
        payment = int(CARGO_PER_UNIT_DEFAULT) + 100_000
        excess = await validate_cargo_payment(
            cargo_key="SomeNewCargo_99",
            payment=payment,
            quantity=1,
            sender_point=None,
            destination_point=None,
        )
        self.assertEqual(excess, 100_000)

    async def test_unknown_cargo_under_default_is_fresh(self):
        excess = await validate_cargo_payment(
            cargo_key="SomeNewCargo_99",
            payment=int(CARGO_PER_UNIT_DEFAULT) - 1,
            quantity=1,
            sender_point=None,
            destination_point=None,
        )
        self.assertEqual(excess, 0)

    async def test_per_unit_fraud_detected(self):
        threshold = CARGO_PER_UNIT_THRESHOLDS["BottlePallete"]
        payment = int(threshold * 10)
        excess = await validate_cargo_payment(
            cargo_key="BottlePallete",
            payment=payment,
            quantity=1,
            sender_point=None,
            destination_point=None,
        )
        self.assertEqual(excess, payment - threshold)

    async def test_per_unit_with_quantity(self):
        threshold = CARGO_PER_UNIT_THRESHOLDS["IronOre"]
        payment = 200_000
        quantity = 8
        excess = await validate_cargo_payment(
            cargo_key="IronOre",
            payment=payment,
            quantity=quantity,
            sender_point=None,
            destination_point=None,
        )
        per_unit = payment / quantity
        expected_excess = int((per_unit - threshold) * quantity)
        self.assertEqual(excess, expected_excess)

    async def test_absolute_ceiling_exceeded(self):
        ceiling = int(CARGO_MAX_ABSOLUTE_PAYMENT["BottlePallete"])
        payment = ceiling + 100_000
        excess = await validate_cargo_payment(
            cargo_key="BottlePallete",
            payment=payment,
            quantity=1,
            sender_point=None,
            destination_point=None,
        )
        per_unit_excess = payment - CARGO_PER_UNIT_THRESHOLDS["BottlePallete"]
        absolute_excess = payment - ceiling
        self.assertEqual(excess, max(per_unit_excess, absolute_excess))

    async def test_distance_fraud_detected(self):
        sender = DeliveryPoint(
            guid="sender-1",
            name="Mine",
            coord=Point(0, 0, 0, srid=3857),
        )
        dest = DeliveryPoint(
            guid="dest-1",
            name="Factory",
            coord=Point(100_000, 0, 0, srid=3857),
        )
        excess = await validate_cargo_payment(
            cargo_key="BottlePallete",
            payment=500_000,
            quantity=1,
            sender_point=sender,
            destination_point=dest,
        )
        self.assertGreater(excess, 0)

    async def test_distance_legitimate_no_excess(self):
        sender = DeliveryPoint(
            guid="sender-2",
            name="Mine",
            coord=Point(0, 0, 0, srid=3857),
        )
        dest = DeliveryPoint(
            guid="dest-2",
            name="Factory",
            coord=Point(100_000, 0, 0, srid=3857),
        )
        excess = await validate_cargo_payment(
            cargo_key="BottlePallete",
            payment=5_000,
            quantity=1,
            sender_point=sender,
            destination_point=dest,
        )
        self.assertEqual(excess, 0)

    async def test_per_unit_skipped_when_perkm_passes_on_long_route(self):
        """Regression (2026-09-22): a long legitimate route paying above the
        static per-unit ceiling but well under the per-km ceiling must not
        be clawed.  CabbagePallet: 926 km route, $30,569 = $33/km, under the
        $200/km ceiling, but $5,569 over the $25k/unit ceiling."""
        sender = DeliveryPoint(
            guid="sender-4",
            name="Rest Area",
            coord=Point(0, 0, 0, srid=3857),
        )
        dest = DeliveryPoint(
            guid="dest-4",
            name="Supermarket",
            coord=Point(926_000, 0, 0, srid=3857),
        )
        unit_threshold = int(CARGO_PER_UNIT_THRESHOLDS["CabbagePallet"])
        payment = unit_threshold + 5_000  # over per-unit, under per-km
        excess = await validate_cargo_payment(
            cargo_key="CabbagePallet",
            payment=payment,
            quantity=1,
            sender_point=sender,
            destination_point=dest,
        )
        self.assertEqual(excess, 0)

    async def test_perkm_still_claws_inflated_long_route(self):
        """Dropping the per-unit check on distance-known routes must not
        disarm long-route inflation: payment per km over the per-km ceiling
        still claws."""
        sender = DeliveryPoint(
            guid="sender-5",
            name="Rest Area",
            coord=Point(0, 0, 0, srid=3857),
        )
        dest = DeliveryPoint(
            guid="dest-5",
            name="Supermarket",
            coord=Point(926_000, 0, 0, srid=3857),
        )
        km_threshold = 200  # CARGO_PER_KM_THRESHOLDS["CabbagePallet"]
        payment = km_threshold * 926 * 10  # 10x the per-km ceiling
        excess = await validate_cargo_payment(
            cargo_key="CabbagePallet",
            payment=payment,
            quantity=1,
            sender_point=sender,
            destination_point=dest,
        )
        expected = int((payment / 926.0 - km_threshold) * 926.0)
        # haversine distance differs from 926 km by a rounding sliver
        self.assertAlmostEqual(excess, expected, delta=2_000)

    async def test_per_unit_applies_without_perkm_threshold(self):
        """Cargo without a per-km ceiling keeps the per-unit check even when
        distance is known (no distance-aware control exists for it)."""
        sender = DeliveryPoint(
            guid="sender-6",
            name="A",
            coord=Point(0, 0, 0, srid=3857),
        )
        dest = DeliveryPoint(
            guid="dest-6",
            name="B",
            coord=Point(50_000, 0, 0, srid=3857),
        )
        assert "Acetone" in CARGO_PER_UNIT_THRESHOLDS
        assert "Acetone" not in CARGO_PER_KM_THRESHOLDS
        threshold = CARGO_PER_UNIT_THRESHOLDS["Acetone"]
        payment = int(threshold * 3)
        excess = await validate_cargo_payment(
            cargo_key="Acetone",
            payment=payment,
            quantity=1,
            sender_point=sender,
            destination_point=dest,
        )
        self.assertEqual(excess, payment - threshold)

    async def _seed_route_history(
        self, sender, dest, payments, cargo="CabbagePallet", weight=0.0
    ):
        """Create ServerCargoArrivedLog history rows for one route pair."""
        from datetime import datetime, timezone as dt_tz

        await sender.asave()
        await dest.asave()

        for i, pay in enumerate(payments):
            await ServerCargoArrivedLog.objects.acreate(
                timestamp=datetime(2026, 9, 1, 12, 0, i, tzinfo=dt_tz.utc),
                cargo_key=cargo,
                payment=pay,
                weight=weight,
                data={"Net_Payment": pay, "Net_CargoKey": cargo},
                sender_point=sender,
                destination_point=dest,
            )

    async def test_route_history_primary_passes_route_consensus(self):
        """A payment above the static per-unit ceiling but within the
        historical consensus of the same route+cargo is not clawed.  This is
        the 2026-09-22 kaizu CabbagePallet case with an established route."""
        sender = DeliveryPoint(
            guid="sender-h1", name="Rest Area", coord=Point(0, 0, 0, srid=3857)
        )
        dest = DeliveryPoint(
            guid="dest-h1", name="Supermarket", coord=Point(926_000, 0, 0, srid=3857)
        )
        await self._seed_route_history(
            sender, dest, [30_000 + (i % 5) * 100 for i in range(25)]
        )
        excess = await validate_cargo_payment(
            cargo_key="CabbagePallet",
            payment=30_569,
            quantity=1,
            sender_point=sender,
            destination_point=dest,
        )
        self.assertEqual(excess, 0)

    async def test_route_history_primary_claws_inflation(self):
        """A payment far above the same route+cargo consensus claws the
        excess over max(2 x p99, 1.2 x max) of the history."""
        sender = DeliveryPoint(
            guid="sender-h2", name="Mine", coord=Point(0, 0, 0, srid=3857)
        )
        dest = DeliveryPoint(
            guid="dest-h2", name="Factory", coord=Point(50_000, 0, 0, srid=3857)
        )
        history = [3_000 + i for i in range(25)]
        await self._seed_route_history(sender, dest, history)
        payment = 100_000
        # p99 index = int(0.99 * (25 - 1)) = 23; ceiling = max(2*p99, 1.2*max)
        route_threshold = max(2 * (3_000 + 23), int(1.2 * (3_000 + 24)))
        excess = await validate_cargo_payment(
            cargo_key="CabbagePallet",
            payment=payment,
            quantity=1,
            sender_point=sender,
            destination_point=dest,
        )
        self.assertEqual(excess, payment - route_threshold)

    async def test_route_history_weight_band_sparse_disables_detection(self):
        """2026-09-24 Moonshine false positive: the route history holds only
        LIGHT jobs while the suspect is a ~100 t full load paying more than
        the weight-blind 2xp99 ceiling.  Detection must be DISABLED (no
        claw), not fall through to the distance-blind per-km/per-unit
        fallbacks which would clip even harder."""
        sender = DeliveryPoint(
            guid="sender-h5", name="Mine", coord=Point(0, 0, 0, srid=3857)
        )
        dest = DeliveryPoint(
            guid="dest-h5", name="Rest Area", coord=Point(1_000_000, 0, 0, srid=3857)
        )
        # 25 light samples (~30 kg units, low pay) — enough for the old
        # weight-blind consensus, but EMPTY in the suspect's weight band.
        await self._seed_route_history(
            sender, dest, [10_000 + i for i in range(25)], weight=30.0
        )
        # Moonshine full load: $88,555/unit at ~100 t, 10 km route.  Per-km
        # fallback ($1000/km) would claw $78,555 and per-unit ($55k) $33,555
        # — neither may apply on a sparse weight band.
        excess = await validate_cargo_payment(
            cargo_key="Moonshine",
            payment=88_555,
            quantity=1,
            sender_point=sender,
            destination_point=dest,
            unit_weight=99.5,
        )
        self.assertEqual(excess, 0)

    async def test_route_history_weight_band_uses_matching_samples(self):
        """A weight-matched consensus is built from same-load samples only:
        a full load is measured against the route's full loads, not against
        its light jobs."""
        sender = DeliveryPoint(
            guid="sender-h6", name="Mine", coord=Point(0, 0, 0, srid=3857)
        )
        dest = DeliveryPoint(
            guid="dest-h6", name="Supermarket", coord=Point(1_000_000, 0, 0, srid=3857)
        )
        # 20 light samples + 20 heavy samples of the same cargo/route.
        await self._seed_route_history(
            sender, dest, [3_000 + i for i in range(20)], weight=30.0
        )
        await self._seed_route_history(
            sender, dest, [50_000 + i for i in range(20)], weight=99.0
        )
        # Full load slightly above the heavy consensus (50k): in band, and
        # far above the light-only ceiling the weight-blind check would use.
        excess = await validate_cargo_payment(
            cargo_key="CabbagePallet",
            payment=55_000,
            quantity=1,
            sender_point=sender,
            destination_point=dest,
            unit_weight=99.5,
        )
        self.assertEqual(excess, 0)
        # ... but genuinely inflated relative to its own load class still claws.
        excess = await validate_cargo_payment(
            cargo_key="CabbagePallet",
            payment=200_000,
            quantity=1,
            sender_point=sender,
            destination_point=dest,
            unit_weight=99.5,
        )
        # heavy p99 = 50_018, max = 50_019 -> ceiling = max(100_036, 60_022)
        self.assertEqual(excess, 200_000 - 100_036)

    async def test_route_history_without_weight_keeps_legacy_behaviour(self):
        """No unit weight on the delivery -> weight-blind consensus, exactly
        the pre-weight-band behaviour."""
        sender = DeliveryPoint(
            guid="sender-h7", name="Mine", coord=Point(0, 0, 0, srid=3857)
        )
        dest = DeliveryPoint(
            guid="dest-h7", name="Factory", coord=Point(50_000, 0, 0, srid=3857)
        )
        history = [3_000 + i for i in range(25)]
        await self._seed_route_history(sender, dest, history)
        payment = 100_000
        route_threshold = max(2 * (3_000 + 23), int(1.2 * (3_000 + 24)))
        excess = await validate_cargo_payment(
            cargo_key="CabbagePallet",
            payment=payment,
            quantity=1,
            sender_point=sender,
            destination_point=dest,
        )
        self.assertEqual(excess, payment - route_threshold)

    async def test_route_history_below_min_samples_falls_back(self):
        """Too few samples = no usable consensus: the per-km fallback
        applies and a modest per-km payment is not clawed even though it is
        above the static per-unit ceiling."""
        sender = DeliveryPoint(
            guid="sender-h3", name="Rest Area", coord=Point(0, 0, 0, srid=3857)
        )
        dest = DeliveryPoint(
            guid="dest-h3", name="Supermarket", coord=Point(926_000, 0, 0, srid=3857)
        )
        await self._seed_route_history(
            sender, dest, [2_000 + i for i in range(ROUTE_HISTORY_MIN_SAMPLES - 1)]
        )
        excess = await validate_cargo_payment(
            cargo_key="CabbagePallet",
            payment=30_569,
            quantity=1,
            sender_point=sender,
            destination_point=dest,
        )
        self.assertEqual(excess, 0)

    async def test_route_history_excludes_marked_clawed_rows(self):
        """Rows MARKED with an amc_fraud_excess claw must not raise the
        consensus ceiling: otherwise a cheat could seed a fresh route with
        20 inflated deliveries at the per-km ceiling and double its own
        future threshold."""
        sender = DeliveryPoint(
            guid="sender-h4", name="Rest Area", coord=Point(0, 0, 0, srid=3857)
        )
        dest = DeliveryPoint(
            guid="dest-h4", name="Supermarket", coord=Point(926_000, 0, 0, srid=3857)
        )
        from datetime import datetime, timezone as dt_tz

        await sender.asave()
        await dest.asave()
        for i in range(25):
            await ServerCargoArrivedLog.objects.acreate(
                timestamp=datetime(2026, 9, 1, 12, 0, i, tzinfo=dt_tz.utc),
                cargo_key="CabbagePallet",
                payment=5_000,  # post-clawback
                data={
                    "Net_Payment": 40_000,
                    "Net_CargoKey": "CabbagePallet",
                    "amc_fraud_excess": 35_000,
                },
                sender_point=sender,
                destination_point=dest,
            )
        # With exclusion: <20 usable samples -> per-km fallback, $97/km < $200.
        # Without exclusion: ceiling ~$79,800 would claw $10,200.
        excess = await validate_cargo_payment(
            cargo_key="CabbagePallet",
            payment=90_000,
            quantity=1,
            sender_point=sender,
            destination_point=dest,
        )
        self.assertEqual(excess, 0)

    async def test_route_history_includes_unmarked_legacy_clawed_rows(self):
        """Pre-marker rows clawed by over-tight historical ceilings (stored
        payment below raw, no amc_fraud_excess marker) keep their RAW
        payment in the consensus: excluding them censors the legitimate top
        of the distribution and false-claws later legitimate deliveries.
        Regression for the 2026-09-22 Moonshine case: the pre-#117 $20k
        per-unit cap clamped every legit full-load run (raw up to $70,401)
        to exactly $20k, the exclusion then left history max at $19,752 and
        a $39,504 ceiling clawed a legit $51,228 run."""
        sender = DeliveryPoint(
            guid="sender-h5", name="Mine", coord=Point(0, 0, 0, srid=3857)
        )
        dest = DeliveryPoint(
            guid="dest-h5", name="Still", coord=Point(926_000, 0, 0, srid=3857)
        )
        from datetime import datetime, timezone as dt_tz

        await sender.asave()
        await dest.asave()
        # 25 unmarked legacy rows: 24 light runs at ~$3,000 plus one legit
        # full-load run at $70,401 — all stored clamped to 20,000
        for i in range(24):
            await ServerCargoArrivedLog.objects.acreate(
                timestamp=datetime(2026, 9, 1, 12, 0, i, tzinfo=dt_tz.utc),
                cargo_key="Moonshine",
                payment=20_000,
                data={"Net_Payment": 3_000, "Net_CargoKey": "Moonshine"},
                sender_point=sender,
                destination_point=dest,
            )
        await ServerCargoArrivedLog.objects.acreate(
            timestamp=datetime(2026, 9, 1, 12, 0, 24, tzinfo=dt_tz.utc),
            cargo_key="Moonshine",
            payment=20_000,
            data={"Net_Payment": 70_401, "Net_CargoKey": "Moonshine"},
            sender_point=sender,
            destination_point=dest,
        )
        # A $51,228 delivery (kaizu's shape) is under 1.2 x 70,401 -> no claw.
        excess = await validate_cargo_payment(
            cargo_key="Moonshine",
            payment=51_228,
            quantity=1,
            sender_point=sender,
            destination_point=dest,
        )
        self.assertEqual(excess, 0)
        # ...but beyond 1.2 x the raw max it still claws.
        excess = await validate_cargo_payment(
            cargo_key="Moonshine",
            payment=90_000,
            quantity=1,
            sender_point=sender,
            destination_point=dest,
        )
        self.assertEqual(excess, int(90_000 - 1.2 * 70_401))

    async def test_short_distance_skipped(self):
        sender = DeliveryPoint(
            guid="sender-3",
            name="A",
            coord=Point(0, 0, 0, srid=3857),
        )
        dest = DeliveryPoint(
            guid="dest-3",
            name="B",
            coord=Point(100, 0, 0, srid=3857),
        )
        excess = await validate_cargo_payment(
            cargo_key="BottlePallete",
            payment=5_000,
            quantity=1,
            sender_point=sender,
            destination_point=dest,
        )
        self.assertEqual(excess, 0)


# ---------------------------------------------------------------------------
# Pure function tests — validate_passenger_payment (sync)
# ---------------------------------------------------------------------------


class ValidatePassengerPaymentTests(TestCase):
    def test_legitimate_taxi(self):
        self.assertEqual(validate_passenger_payment(2, 50_000), 0)

    def test_exceeds_taxi_ceiling(self):
        ceiling = PASSENGER_PAYMENT_CEILINGS[2]
        self.assertEqual(validate_passenger_payment(2, ceiling + 100_000), 100_000)

    def test_hitchhiker_exceeds_ceiling(self):
        ceiling = PASSENGER_PAYMENT_CEILINGS[1]
        payment = 5_000
        self.assertEqual(validate_passenger_payment(1, payment), payment - ceiling)

    def test_unknown_passenger_type_returns_zero(self):
        self.assertEqual(validate_passenger_payment(99, 999_999), 0)

    def test_zero_payment_returns_zero(self):
        self.assertEqual(validate_passenger_payment(2, 0), 0)


# ---------------------------------------------------------------------------
# Pure function tests — validate_tow_payment (sync)
# ---------------------------------------------------------------------------


class ValidateTowPaymentTests(TestCase):
    def test_legitimate_tow(self):
        self.assertEqual(validate_tow_payment(50_000), 0)

    def test_exceeds_ceiling(self):
        self.assertEqual(validate_tow_payment(TOW_PAYMENT_CEILING + 50_000), 50_000)

    def test_zero_payment_returns_zero(self):
        self.assertEqual(validate_tow_payment(0), 0)


# ---------------------------------------------------------------------------
# Integration tests — cargo fraud detection through process_event
# ---------------------------------------------------------------------------


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
class FraudCargoIntegrationTests(TestCase):
    async def _setup(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character,
            location=Point(0, 0, 0),
            vehicle_key="TestVehicle",
        )
        await DeliveryPoint.objects.acreate(
            guid="fs", name="Mine", coord=Point(0, 0, 0)
        )
        await DeliveryPoint.objects.acreate(
            guid="fd", name="Factory", coord=Point(100_000, 0, 0)
        )
        return player, character

    def _cargo_event(self, character, player, cargo_key, payment):
        return {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "Cargos": [
                    {
                        "Net_CargoKey": cargo_key,
                        "Net_Payment": payment,
                        "Net_Weight": 100.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100_000, "Y": 0, "Z": 0},
                    }
                ],
                "PlayerId": str(player.unique_id),
                "CharacterGuid": str(character.guid),
            },
        }

    async def test_legitimate_no_reduction(self, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        player, character = await self._setup()

        base_pay, _, _, _ = await process_event(
            self._cargo_event(character, player, "BottlePallete", 5_000),
            player,
            character,
        )
        log = await ServerCargoArrivedLog.objects.afirst()
        self.assertEqual(log.payment, 5_000)
        self.assertEqual(base_pay, 5_000)

    async def test_inflated_reduces_log_payment(self, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        player, character = await self._setup()

        await process_event(
            self._cargo_event(character, player, "BottlePallete", 500_000),
            player,
            character,
        )
        log = await ServerCargoArrivedLog.objects.afirst()
        threshold = CARGO_PER_UNIT_THRESHOLDS["BottlePallete"]
        # Payment is reduced to AT most the per-unit threshold; the max()
        # over (distance, per-unit, absolute) can claw slightly more.
        self.assertLessEqual(log.payment, threshold)

    async def test_inflated_reduces_delivery_payment(self, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        player, character = await self._setup()

        await process_event(
            self._cargo_event(character, player, "BottlePallete", 500_000),
            player,
            character,
        )
        delivery = await Delivery.objects.afirst()
        threshold = CARGO_PER_UNIT_THRESHOLDS["BottlePallete"]
        self.assertLessEqual(delivery.payment, threshold)

    async def test_inflated_reduces_base_pay(self, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        player, character = await self._setup()

        base_pay, _, _, clawback = await process_event(
            self._cargo_event(character, player, "BottlePallete", 500_000),
            player,
            character,
        )
        threshold = CARGO_PER_UNIT_THRESHOLDS["BottlePallete"]
        # base_pay includes the clawback amount (process_events subtracts it).
        self.assertEqual(base_pay, 500_000)
        # The max() over (distance, per-unit, absolute) claws at least the
        # per-unit excess — and may claw slightly more via the distance branch.
        self.assertGreaterEqual(clawback, 500_000 - threshold)

    async def test_multiple_cargos_each_validated(self, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        player, character = await self._setup()

        threshold = CARGO_PER_UNIT_THRESHOLDS["BottlePallete"]
        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "Cargos": [
                    {
                        "Net_CargoKey": "BottlePallete",
                        "Net_Payment": 500_000,
                        "Net_Weight": 100.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100_000, "Y": 0, "Z": 0},
                    },
                    {
                        "Net_CargoKey": "BottlePallete",
                        "Net_Payment": 5_000,
                        "Net_Weight": 100.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100_000, "Y": 0, "Z": 0},
                    },
                ],
                "PlayerId": str(player.unique_id),
                "CharacterGuid": str(character.guid),
            },
        }
        base_pay, _, _, clawback = await process_event(event, player, character)

        logs = [log async for log in ServerCargoArrivedLog.objects.all()]
        self.assertEqual(len(logs), 2)
        payments = sorted(log.payment for log in logs)
        self.assertEqual(payments[0], 5_000)
        self.assertLessEqual(payments[1], threshold)
        # base_pay includes the clawback amount (process_events subtracts it);
        # the max() over (distance, per-unit, absolute) may claw slightly more
        # than the bare per-unit excess.
        self.assertLessEqual(base_pay - clawback, threshold + 5_000)


# ---------------------------------------------------------------------------
# Integration tests — passenger fraud detection through process_event
# ---------------------------------------------------------------------------


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
class FraudPassengerIntegrationTests(TestCase):
    async def _setup(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character,
            location=Point(0, 0, 0),
            vehicle_key="TestVehicle",
        )
        return player, character

    def _passenger_event(self, player, ptype, payment):
        return {
            "hook": "ServerPassengerArrived",
            "timestamp": int(time.time()),
            "data": {
                "Passenger": {
                    "Net_PassengerType": ptype,
                    "Net_Payment": payment,
                    "Net_bArrived": True,
                    "Net_Distance": 10_000,
                    "Net_StartLocation": {"X": 100, "Y": 100, "Z": 100},
                    "Net_DestinationLocation": {"X": 200, "Y": 200, "Z": 200},
                },
                "PlayerId": str(player.unique_id),
            },
        }

    async def test_legitimate_no_reduction(self, mock_treasury, mock_rp):
        mock_rp.return_value = False
        player, character = await self._setup()

        event = self._passenger_event(player, ptype=2, payment=50_000)
        base_pay, _, _, _ = await process_event(event, player, character)

        log = await ServerPassengerArrivedLog.objects.afirst()
        self.assertIsNotNone(log)
        self.assertGreaterEqual(log.payment, 50_000)
        self.assertEqual(base_pay, log.payment)

    async def test_inflated_taxi_reduces_payment(self, mock_treasury, mock_rp):
        mock_rp.return_value = False
        player, character = await self._setup()

        event = self._passenger_event(player, ptype=2, payment=10_000_000)
        base_pay, _, _, _ = await process_event(event, player, character)

        log = await ServerPassengerArrivedLog.objects.afirst()
        ceiling = PASSENGER_PAYMENT_CEILINGS[2]
        self.assertEqual(log.payment, ceiling)
        # base_pay includes the clawback amount (process_events subtracts it).
        self.assertEqual(base_pay, 10_000_000)

    async def test_inflated_hitchhiker_detected(self, mock_treasury, mock_rp):
        mock_rp.return_value = False
        player, character = await self._setup()

        event = self._passenger_event(player, ptype=1, payment=5_000)
        base_pay, _, _, _ = await process_event(event, player, character)

        log = await ServerPassengerArrivedLog.objects.afirst()
        self.assertEqual(log.payment, PASSENGER_PAYMENT_CEILINGS[1])
        # base_pay includes the clawback amount (process_events subtracts it).
        self.assertEqual(base_pay, 5_000)


# ---------------------------------------------------------------------------
# Integration tests — tow fraud detection through process_event
# ---------------------------------------------------------------------------


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
class FraudTowIntegrationTests(TestCase):
    async def _setup(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character,
            location=Point(0, 0, 0),
            vehicle_key="TestVehicle",
        )
        return player, character

    def _tow_event(self, player, payment, flags=1, body_damage=None):
        tow_data = {"Net_TowRequestFlags": flags, "Net_Payment": payment}
        if body_damage is not None:
            tow_data["BodyDamage"] = body_damage
        return {
            "hook": "ServerTowRequestArrived",
            "timestamp": int(time.time()),
            "data": {"TowRequest": tow_data, "PlayerId": str(player.unique_id)},
        }

    async def test_legitimate_no_reduction(self, mock_treasury, mock_rp):
        mock_rp.return_value = False
        player, character = await self._setup()

        base_pay, subsidy, _, _ = await process_event(
            self._tow_event(player, payment=10_000),
            player,
            character,
        )
        log = await ServerTowRequestArrivedLog.objects.afirst()
        self.assertEqual(log.payment, 10_000)
        self.assertEqual(base_pay, 10_000)

    async def test_inflated_reduces_payment(self, mock_treasury, mock_rp):
        mock_rp.return_value = False
        player, character = await self._setup()

        base_pay, _, _, _ = await process_event(
            self._tow_event(player, payment=1_000_000, body_damage=1.0),
            player,
            character,
        )
        log = await ServerTowRequestArrivedLog.objects.afirst()
        self.assertEqual(log.payment, TOW_PAYMENT_CEILING)
        # base_pay includes the clawback amount (process_events subtracts it).
        self.assertEqual(base_pay, 1_000_000)

    async def test_inflated_reduces_subsidy(self, mock_treasury, mock_rp):
        mock_rp.return_value = False
        player, character = await self._setup()

        _, subsidy, _, _ = await process_event(
            self._tow_event(player, payment=1_000_000, body_damage=1.0),
            player,
            character,
        )
        expected = 2_000 + TOW_PAYMENT_CEILING * 1.0
        self.assertEqual(subsidy, expected)


# ---------------------------------------------------------------------------
# Discord alert wiring for fraud clawbacks
# ---------------------------------------------------------------------------


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
class FraudAlertWiringTests(TestCase):
    """Fraud clawbacks must surface a Discord alert, not just a log line."""

    async def _setup(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character,
            location=Point(0, 0, 0),
            vehicle_key="TestVehicle",
        )
        return player, character

    @staticmethod
    def _passenger_event(player, ptype, payment, start):
        return {
            "hook": "ServerPassengerArrived",
            "timestamp": int(time.time()),
            "data": {
                "Passenger": {
                    "Net_PassengerType": ptype,
                    "Net_Payment": payment,
                    "Net_bArrived": True,
                    "Net_Distance": 10_000,
                    "Net_StartLocation": start,
                    "Net_DestinationLocation": {"X": 200, "Y": 200, "Z": 200},
                },
                "PlayerId": str(player.unique_id),
            },
        }

    @patch("amc.handlers.passenger.post_discord_fraud_alert")
    async def test_inflated_taxi_posts_alert(self, mock_alert, mock_treasury, mock_rp):
        mock_rp.return_value = False
        player, character = await self._setup()

        base_pay, _, _, clawback = await process_event(
            self._passenger_event(
                player, 2, 10_000_000, {"X": 100, "Y": 100, "Z": 100}
            ),
            player,
            character,
        )

        # Contract: base_pay includes the clawback amount.
        self.assertEqual(base_pay, 10_000_000)
        self.assertEqual(clawback, 10_000_000 - PASSENGER_PAYMENT_CEILINGS[2])

        mock_alert.assert_called_once()
        kwargs = mock_alert.call_args.kwargs
        self.assertEqual(kwargs["kind"], "passenger_over_ceiling")
        self.assertEqual(kwargs["original_payment"], 10_000_000)
        self.assertEqual(
            kwargs["clawed_back"], 10_000_000 - PASSENGER_PAYMENT_CEILINGS[2]
        )
        self.assertEqual(kwargs["final_payment"], PASSENGER_PAYMENT_CEILINGS[2])

    @patch("amc.handlers.passenger.post_discord_fraud_alert")
    async def test_legitimate_passenger_no_alert(
        self, mock_alert, mock_treasury, mock_rp
    ):
        mock_rp.return_value = False
        player, character = await self._setup()

        await process_event(
            self._passenger_event(player, 2, 50_000, {"X": 100, "Y": 100, "Z": 100}),
            player,
            character,
        )

        mock_alert.assert_not_called()

    @patch("amc.handlers.passenger.post_discord_fraud_alert")
    async def test_zero_origin_passenger_pays_normally(
        self, mock_alert, mock_treasury, mock_rp
    ):
        # Zero-origin rejection was removed: a passenger with
        # Net_StartLocation (0,0,0) is processed like any other delivery.
        mock_rp.return_value = False
        player, character = await self._setup()

        base_pay, _, _, clawback = await process_event(
            self._passenger_event(player, 2, 50_000, {"X": 0, "Y": 0, "Z": 0}),
            player,
            character,
            http_client_mod=MagicMock(),
        )

        self.assertEqual(clawback, 0)
        self.assertEqual(base_pay, 50_000)
        mock_alert.assert_not_called()

    @patch("amc.handlers.tow.post_discord_fraud_alert")
    async def test_inflated_tow_posts_alert(self, mock_alert, mock_treasury, mock_rp):
        mock_rp.return_value = False
        player, character = await self._setup()

        tow_data = {
            "Net_TowRequestFlags": 1,
            "Net_Payment": 1_000_000,
            "BodyDamage": 1.0,
        }
        event = {
            "hook": "ServerTowRequestArrived",
            "timestamp": int(time.time()),
            "data": {"TowRequest": tow_data, "PlayerId": str(player.unique_id)},
        }
        await process_event(event, player, character)

        mock_alert.assert_called_once()
        kwargs = mock_alert.call_args.kwargs
        self.assertEqual(kwargs["kind"], "tow_over_ceiling")
        self.assertEqual(kwargs["original_payment"], 1_000_000)
        self.assertEqual(kwargs["clawed_back"], 1_000_000 - TOW_PAYMENT_CEILING)
        self.assertEqual(kwargs["final_payment"], TOW_PAYMENT_CEILING)

    @patch("amc.handlers.cargo.post_discord_fraud_alert")
    async def test_inflated_cargo_posts_alert(self, mock_alert, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        player, character = await self._setup()

        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "Cargos": [
                    {
                        "Net_CargoKey": "BottlePallete",
                        "Net_Payment": 500_000,
                        "Net_Weight": 100.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100_000, "Y": 0, "Z": 0},
                    },
                ],
                "PlayerId": str(player.unique_id),
                "CharacterGuid": str(character.guid),
            },
        }
        base_pay, _, _, clawback = await process_event(event, player, character)

        # Contract: base_pay includes the clawback amount.
        self.assertGreater(clawback, 0)
        self.assertEqual(
            base_pay - clawback, CARGO_PER_UNIT_THRESHOLDS["BottlePallete"]
        )

        mock_alert.assert_called_once()
        kwargs = mock_alert.call_args.kwargs
        self.assertEqual(kwargs["kind"], "cargo_over_threshold")
        self.assertGreater(kwargs["clawed_back"], 0)
        self.assertEqual(
            kwargs["final_payment"], CARGO_PER_UNIT_THRESHOLDS["BottlePallete"]
        )


class PostDiscordFraudAlertTests(TestCase):
    """post_discord_fraud_alert: no-op without config, never raises."""

    def _alert(self, client):
        post_discord_fraud_alert(
            client,
            kind="passenger_over_ceiling",
            character_name="Tester",
            player_id="123",
            original_payment=10_000_000,
            clawed_back=9_800_000,
            final_payment=200_000,
            detail="test",
        )

    def test_noop_without_channel(self):
        client = MagicMock()
        with override_settings(DISCORD_FRAUD_ALERT_CHANNEL_ID=0):
            self._alert(client)
        client.get_channel.assert_not_called()

    def test_noop_without_client(self):
        with override_settings(DISCORD_FRAUD_ALERT_CHANNEL_ID=123):
            self._alert(None)

    @patch("amc.pipeline.discord.asyncio")
    def test_schedules_embed_send(self, mock_asyncio):
        client = MagicMock()
        channel = MagicMock()
        client.get_channel.return_value = channel
        with override_settings(DISCORD_FRAUD_ALERT_CHANNEL_ID=123):
            self._alert(client)

        client.get_channel.assert_called_once_with(123)
        mock_asyncio.run_coroutine_threadsafe.assert_called_once()
        channel.send.assert_called_once()
        embed = channel.send.call_args.kwargs["embed"]
        self.assertEqual(embed.title, "Fraud clawback")

    def test_missing_channel_does_not_raise(self):
        client = MagicMock()
        client.get_channel.return_value = None
        with override_settings(DISCORD_FRAUD_ALERT_CHANNEL_ID=123):
            self._alert(client)

    def test_client_error_is_swallowed(self):
        client = MagicMock()
        client.get_channel.side_effect = RuntimeError("boom")
        with override_settings(DISCORD_FRAUD_ALERT_CHANNEL_ID=123):
            self._alert(client)


# ---------------------------------------------------------------------------
# Fraud-marked batches disable the earnings (savings) deposit
# ---------------------------------------------------------------------------


class FraudSavingsSkipTests(TestCase):
    """on_player_profit skips the savings sweep for fraud-marked batches."""

    async def _character(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        return character

    @patch("amc.pipeline.profit.set_aside_player_savings", new_callable=AsyncMock)
    @patch("amc.pipeline.profit.repay_loan_for_profit", new_callable=AsyncMock)
    async def test_fraud_marked_skips_savings(self, mock_repay, mock_savings):
        mock_repay.return_value = 0
        character = await self._character()

        await on_player_profit(
            character, 0, 100_000, MagicMock(), fraud_marked=True
        )

        mock_savings.assert_not_awaited()

    @patch("amc.pipeline.profit.set_aside_player_savings", new_callable=AsyncMock)
    @patch("amc.pipeline.profit.repay_loan_for_profit", new_callable=AsyncMock)
    async def test_clean_batch_still_sweeps_savings(
        self, mock_repay, mock_savings
    ):
        mock_repay.return_value = 0
        character = await self._character()

        session = MagicMock()
        await on_player_profit(
            character, 0, 100_000, session, fraud_marked=False
        )

        mock_savings.assert_awaited_once_with(character, 100_000, session)

    @patch("amc.pipeline.profit.on_player_profit", new_callable=AsyncMock)
    async def test_on_player_profits_looks_up_fraud_flags(self, mock_profit):
        character = await self._character()
        other = await self._character()

        await on_player_profits(
            [(character, 0, 100, 0), (other, 0, 100, 0)],
            MagicMock(),
            fraud_flags={character.pk: True},
        )

        self.assertEqual(mock_profit.await_count, 2)
        flagged = mock_profit.await_args_list[0].kwargs["fraud_marked"]
        clean = mock_profit.await_args_list[1].kwargs["fraud_marked"]
        self.assertTrue(flagged)
        self.assertFalse(clean)


# ---------------------------------------------------------------------------
# process_events: fraud batches claw back from the wallet and flag profits
# ---------------------------------------------------------------------------


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
class FraudBatchProcessEventsTests(TestCase):
    async def _setup(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character,
            location=Point(0, 0, 0),
            vehicle_key="TestVehicle",
        )
        return player, character

    @patch("amc.webhook.on_player_profits", new_callable=AsyncMock)
    @patch("amc.webhook.transfer_money", new_callable=AsyncMock)
    async def test_fraud_batch_claws_wallet_and_flags_profits(
        self, mock_transfer, mock_profits, mock_treasury, mock_rp
    ):
        from django.utils import timezone

        from amc.models import PlayerStatusLog

        mock_rp.return_value = False
        player, character = await self._setup()
        await PlayerStatusLog.objects.acreate(
            character=character,
            timespan=(timezone.now() - timedelta(minutes=5), timezone.now()),
        )

        event = {
            "hook": "ServerPassengerArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Passenger": {
                    "Net_PassengerType": 2,
                    "Net_Payment": 10_000_000,
                    "Net_bArrived": True,
                    "Net_Distance": 10_000,
                    "Net_StartLocation": {"X": 100, "Y": 100, "Z": 100},
                    "Net_DestinationLocation": {"X": 200, "Y": 200, "Z": 200},
                },
                "PlayerId": str(player.unique_id),
            },
        }

        await process_events([event], http_client_mod=MagicMock())
        await asyncio.sleep(0)

        mock_transfer.assert_awaited_once()
        args = mock_transfer.await_args.args
        self.assertEqual(args[1], -(10_000_000 - PASSENGER_PAYMENT_CEILINGS[2]))
        self.assertEqual(args[2], "Fraud clawback")

        mock_profits.assert_awaited_once()
        self.assertTrue(
            mock_profits.await_args.kwargs["fraud_flags"][character.pk]
        )
        # Reported batch income is the legitimate portion only.
        _, _, total_base, _ = mock_profits.await_args.args[0][0]
        self.assertEqual(total_base, PASSENGER_PAYMENT_CEILINGS[2])

