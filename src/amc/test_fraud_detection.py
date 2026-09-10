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
    CARGO_PER_UNIT_DEFAULT,
    CARGO_PER_UNIT_THRESHOLDS,
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

    @patch("amc.handlers.passenger.show_popup", new_callable=AsyncMock)
    @patch("amc.handlers.passenger.post_discord_fraud_alert")
    async def test_zero_origin_passenger_posts_alert(
        self, mock_alert, mock_popup, mock_treasury, mock_rp
    ):
        mock_rp.return_value = False
        player, character = await self._setup()

        base_pay, subsidy, contract, clawback = await process_event(
            self._passenger_event(player, 2, 5_000_000, {"X": 0, "Y": 0, "Z": 0}),
            player,
            character,
            http_client_mod=MagicMock(),
        )

        # Contract: base_pay includes the clawback so the batch nets to zero.
        self.assertEqual(
            (base_pay, subsidy, contract, clawback),
            (5_000_000, 0, 0, 5_000_000),
        )
        mock_alert.assert_called_once()
        kwargs = mock_alert.call_args.kwargs
        self.assertEqual(kwargs["kind"], "passenger_zero_origin")
        self.assertEqual(kwargs["original_payment"], 5_000_000)
        self.assertEqual(kwargs["clawed_back"], 5_000_000)
        self.assertEqual(kwargs["final_payment"], 0)

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

