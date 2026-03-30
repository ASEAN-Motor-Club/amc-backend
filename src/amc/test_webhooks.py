import time
import asyncio
from typing import Any
from datetime import timedelta
from unittest.mock import patch, MagicMock, AsyncMock
from django.core.cache import cache
from django.test import TestCase
from django.contrib.gis.geos import Point
import unittest
from asgiref.sync import sync_to_async
from amc.factories import PlayerFactory, CharacterFactory
from amc.webhook import process_events, process_event
from amc.models import (
    DeliveryPoint,
    ServerCargoArrivedLog,
    ServerPassengerArrivedLog,
    ServerTowRequestArrivedLog,
    DeliveryJob,
    ServerSignContractLog,
    CharacterLocation,
    PlayerStatusLog,
    Delivery,
    SubsidyRule,
    Cargo,
)
from decimal import Decimal
from django.utils import timezone


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
class ProcessEventTests(TestCase):
    async def test_process_event(self, mock_get_treasury, mock_get_rp_mode):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        mine = await DeliveryPoint.objects.acreate(
            guid="1",
            name="mine",
            type="mine",
            coord=Point(0, 0, 0),
        )
        factory = await DeliveryPoint.objects.acreate(
            guid="2",
            name="factory",
            type="factory",
            coord=Point(1000, 1000, 0),
        )
        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "Cargos": [
                    {
                        "Net_CargoKey": "oranges",
                        "Net_Payment": 10_000,
                        "Net_Weight": 100.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 1000, "Y": 1000, "Z": 0},
                    }
                ],
                "PlayerId": str(player.unique_id),
                "CharacterGuid": str(character.guid),
            },
        }
        payment, subsidy, _ = await process_event(event, player, character)
        self.assertEqual(await ServerCargoArrivedLog.objects.acount(), 1)
        delivery = await ServerCargoArrivedLog.objects.select_related(
            "player", "sender_point", "destination_point"
        ).afirst()
        self.assertIsNotNone(delivery)
        self.assertEqual(delivery.payment, 10_000)
        self.assertEqual(payment, 10_000)
        self.assertEqual(delivery.cargo_key, "oranges")
        self.assertEqual(delivery.weight, 100.0)
        self.assertEqual(delivery.damage, 0.0)
        self.assertEqual(delivery.player, player)
        self.assertEqual(delivery.sender_point, mine)
        self.assertEqual(delivery.destination_point, factory)

    async def test_taxi(self, mock_get_treasury, mock_get_rp_mode):
        mock_get_rp_mode.return_value = False
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        event = {
            "hook": "ServerPassengerArrived",
            "timestamp": int(time.time()),
            "data": {
                "Passenger": {
                    "Net_PassengerType": 2,
                    "Net_Payment": 10_000,
                    "Net_bArrived": True,
                    "Net_Distance": 10_000,
                    "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                    "Net_DestinationLocation": {"X": 1000, "Y": 1000, "Z": 0},
                    "Net_LCComfortSatisfaction": 5,
                    "Net_TimeLimitPoint": 5,
                },
                "PlayerId": str(player.unique_id),
            },
        }
        payment, subsidy, _ = await process_event(event, player, character)
        self.assertEqual(await ServerPassengerArrivedLog.objects.acount(), 1)
        log = await ServerPassengerArrivedLog.objects.select_related("player").afirst()
        self.assertIsNotNone(log)
        self.assertEqual(log.payment, 10_000)
        self.assertEqual(payment, 10_000)
        self.assertEqual(subsidy, 7_000)
        self.assertEqual(log.player, player)

    async def test_ambulance_with_radius_ratio(self, mock_get_treasury, mock_get_rp_mode):
        """Ambulance with radius ratio 0.2 → +80% bonus on base payment."""
        mock_get_rp_mode.return_value = False
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        event = {
            "hook": "ServerPassengerArrived",
            "timestamp": int(time.time()),
            "data": {
                "Passenger": {
                    "Net_PassengerType": 3,  # Ambulance
                    "Net_Payment": 10_000,
                    "Net_bArrived": True,
                    "Net_Distance": 5_000,
                    "Net_SearchAndRescueRadiusRatio": 0.2,
                    "Net_LCComfortSatisfaction": 0,
                    "Net_TimeLimitPoint": 0,
                },
                "PlayerId": str(player.unique_id),
            },
        }
        payment, subsidy, _ = await process_event(event, player, character)
        self.assertEqual(await ServerPassengerArrivedLog.objects.acount(), 1)
        log = await ServerPassengerArrivedLog.objects.select_related("player").afirst()
        self.assertIsNotNone(log)
        # base 10,000 + bonus int(10,000 * 0.8) = 18,000
        self.assertEqual(log.payment, 18_000)
        # subsidy: 2,000 + 18,000 * 0.5 = 11,000
        self.assertEqual(subsidy, 11_000)
        self.assertEqual(payment, 18_000)

    async def test_ambulance_without_radius_ratio(self, mock_get_treasury, mock_get_rp_mode):
        """Ambulance without radius ratio field (backward compat) — no bonus, only subsidy."""
        mock_get_rp_mode.return_value = False
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        event = {
            "hook": "ServerPassengerArrived",
            "timestamp": int(time.time()),
            "data": {
                "Passenger": {
                    "Net_PassengerType": 3,  # Ambulance
                    "Net_Payment": 10_000,
                    "Net_bArrived": True,
                    "Net_Distance": 5_000,
                    "Net_LCComfortSatisfaction": 0,
                    "Net_TimeLimitPoint": 0,
                },
                "PlayerId": str(player.unique_id),
            },
        }
        payment, subsidy, _ = await process_event(event, player, character)
        log = await ServerPassengerArrivedLog.objects.select_related("player").afirst()
        self.assertIsNotNone(log)
        # No radius ratio → no bonus, payment stays at base
        self.assertEqual(log.payment, 10_000)
        # subsidy: 2,000 + 10,000 * 0.5 = 7,000
        self.assertEqual(subsidy, 7_000.0)
        self.assertEqual(payment, 10_000)

    async def test_tow(self, mock_get_treasury, mock_get_rp_mode):
        """Tow with no body damage info (backward compat) — BodyDamage defaults to 1.0, no bonus."""
        mock_get_rp_mode.return_value = False
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        event = {
            "hook": "ServerTowRequestArrived",
            "timestamp": int(time.time()),
            "data": {
                "TowRequest": {
                    "Net_TowRequestFlags": 1,
                    "Net_Payment": 10_000,
                },
                "PlayerId": str(player.unique_id),
            },
        }
        payment, subsidy, _ = await process_event(event, player, character)
        self.assertEqual(await ServerTowRequestArrivedLog.objects.acount(), 1)
        log = await ServerTowRequestArrivedLog.objects.select_related("player").afirst()
        self.assertIsNotNone(log)
        self.assertEqual(log.payment, 10_000)
        self.assertEqual(payment, 10_000)
        self.assertEqual(subsidy, 12_000)
        self.assertEqual(log.player, player)

    async def test_tow_body_damage_bonus(self, mock_get_treasury, mock_get_rp_mode):
        """Tow with 0 body damage → full bonus (55% of base)."""
        mock_get_rp_mode.return_value = False
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        event = {
            "hook": "ServerTowRequestArrived",
            "timestamp": int(time.time()),
            "data": {
                "TowRequest": {
                    "Net_TowRequestFlags": 0,
                    "Net_Payment": 10_000,
                    "BodyDamage": 0.0,  # 0 damage → full bonus
                },
                "PlayerId": str(player.unique_id),
            },
        }
        payment, subsidy, _ = await process_event(event, player, character)
        log = await ServerTowRequestArrivedLog.objects.select_related("player").afirst()
        # 0 damage: bonus = int(10_000 * 0.55) = 5_500, total payment = 15_500
        self.assertEqual(log.payment, 15_500)
        # subsidy for non-flipped: 2_000 + 15_500 * 0.5 = 9_750
        self.assertEqual(subsidy, 9_750)
        self.assertEqual(payment, 15_500)

    async def test_tow_body_damage_partial(self, mock_get_treasury, mock_get_rp_mode):
        """Tow with partial body damage: bonus scales linearly with (1 - BodyDamage)."""
        mock_get_rp_mode.return_value = False
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        event = {
            "hook": "ServerTowRequestArrived",
            "timestamp": int(time.time()),
            "data": {
                "TowRequest": {
                    "Net_TowRequestFlags": 0,
                    "Net_Payment": 10_000,
                    "BodyDamage": 0.5,  # 50% damage → 50% of max bonus
                },
                "PlayerId": str(player.unique_id),
            },
        }
        payment, subsidy, _ = await process_event(event, player, character)
        log = await ServerTowRequestArrivedLog.objects.select_related("player").afirst()
        # bonus = int(10_000 * 0.55 * 0.5) = 2_750, total = 12_750
        self.assertEqual(log.payment, 12_750)
        self.assertEqual(subsidy, 2_000 + 12_750 * 0.5)
        self.assertEqual(payment, 12_750)

    async def test_rp_mode_subsidy(self, mock_get_treasury, mock_get_rp_mode):
        # Verify subsidy calculation when RP mode is ON
        mock_get_rp_mode.return_value = True
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        # Needs points for delivery creation
        await DeliveryPoint.objects.acreate(guid="1", name="mine", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="2", name="factory", coord=Point(1000, 1000, 0)
        )

        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "Cargos": [
                    {
                        "Net_CargoKey": "oranges",
                        "Net_Payment": 10_000,
                        "Net_Weight": 100.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 1000, "Y": 1000, "Z": 0},
                    }
                ],
                "PlayerId": str(player.unique_id),
            },
        }

        payment, subsidy, _ = await process_event(
            event, player, character, is_rp_mode=True, treasury_balance=100_000
        )

        self.assertEqual(subsidy, 5000)
        self.assertEqual(payment, 10000)

    async def test_job_completion(self, mock_get_treasury, mock_get_rp_mode):
        mock_get_rp_mode.return_value = False

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        p1 = await DeliveryPoint.objects.acreate(
            guid="j1", name="J1", coord=Point(0, 0, 0)
        )
        p2 = await DeliveryPoint.objects.acreate(
            guid="j2", name="J2", coord=Point(100, 100, 0)
        )

        job = await DeliveryJob.objects.acreate(
            name="Test Job",
            cargo_key="apples",
            quantity_requested=10,
            quantity_fulfilled=0,
            completion_bonus=50000,
            bonus_multiplier=1.0,
            expired_at=timezone.now() + timedelta(days=1),
        )
        await job.source_points.aadd(p1)
        await job.destination_points.aadd(p2)

        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "Cargos": [
                    {
                        "Net_CargoKey": "apples",
                        "Net_Payment": 100,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
                "PlayerId": str(player.unique_id),
            },
        }

        await process_event(event, player, character)

        await job.arefresh_from_db()
        self.assertEqual(job.quantity_fulfilled, 1)

    async def test_server_sign_contract(self, mock_get_treasury, mock_get_rp_mode):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        event = {
            "hook": "ServerSignContract",
            "timestamp": int(time.time()),
            "data": {
                "Contract": {
                    "Item": "sand",
                    "Amount": 100,
                    "CompletionPayment": {"BaseValue": 50000},
                    "Cost": {"BaseValue": 1000},
                }
            },
        }

        await process_event(event, player, character)

        self.assertEqual(await ServerSignContractLog.objects.acount(), 1)
        log = await ServerSignContractLog.objects.afirst()
        self.assertIsNotNone(log)
        self.assertEqual(log.cargo_key, "sand")
        self.assertEqual(log.amount, 100)
        self.assertEqual(log.payment, 50000)
        self.assertEqual(log.cost, 1000)

    async def test_contract_delivered(self, mock_get_treasury, mock_get_rp_mode):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        # Create initial contract log
        log = await ServerSignContractLog.objects.acreate(
            guid="contract_guid_123",
            player=player,
            cargo_key="sand",
            amount=2,
            finished_amount=0,
            payment=50000,
            cost=1000,
            timestamp=timezone.now(),
        )

        event = {
            "hook": "ServerContractCargoDelivered",
            "timestamp": int(time.time()),
            "data": {
                "ContractGuid": "contract_guid_123",
                "Item": "sand",
                "Amount": 2,
                "CompletionPayment": 50000,
                "Cost": 1000,
            },
        }

        # First delivery
        await process_event(event, player, character)
        await log.arefresh_from_db()
        self.assertEqual(log.finished_amount, 1)
        self.assertFalse(log.delivered)

        # Second delivery (completion)
        await process_event(event, player, character)
        await log.arefresh_from_db()
        self.assertEqual(log.finished_amount, 2)
        self.assertTrue(log.delivered)

    async def test_contract_new_mod_flow(self, mock_get_treasury, mock_get_rp_mode):
        """New mod: ServerSignContract sends guid, ServerContractCargoDelivered sends guid-only."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        # Step 1: Sign contract (new mod includes ContractGuid)
        sign_event = {
            "hook": "ServerSignContract",
            "timestamp": int(time.time()),
            "data": {
                "ContractGuid": "new_mod_guid_456",
                "Contract": {
                    "Item": "gravel",
                    "Amount": 3,
                    "CompletionPayment": {"BaseValue": 75000},
                    "Cost": {"BaseValue": 2000},
                },
            },
        }
        await process_event(sign_event, player, character)

        log = await ServerSignContractLog.objects.aget(guid="new_mod_guid_456")
        self.assertEqual(log.cargo_key, "gravel")
        self.assertEqual(log.amount, 3)
        self.assertEqual(log.payment, 75000)

        # Step 2: Deliver cargo (new mod sends guid-only, no Item/Amount)
        deliver_event = {
            "hook": "ServerContractCargoDelivered",
            "timestamp": int(time.time()),
            "data": {
                "ContractGuid": "new_mod_guid_456",
            },
        }

        # Deliveries 1, 2 — not complete yet
        for i in range(2):
            _, _, contract_pay = await process_event(deliver_event, player, character)
            self.assertEqual(contract_pay, 0)

        await log.arefresh_from_db()
        self.assertEqual(log.finished_amount, 2)
        self.assertFalse(log.delivered)

        # Delivery 3 — completion
        _, _, contract_pay = await process_event(deliver_event, player, character)
        self.assertEqual(contract_pay, 75000)

        await log.arefresh_from_db()
        self.assertEqual(log.finished_amount, 3)
        self.assertTrue(log.delivered)

    @patch("amc.webhook.detect_custom_parts")
    @patch("amc.webhook.list_player_vehicles", new_callable=AsyncMock)
    @patch("amc.webhook.show_popup", new_callable=AsyncMock)
    @patch("amc.webhook.transfer_money", new_callable=AsyncMock)
    async def test_cargo_arrived_money_modded(self, mock_transfer, mock_show_popup, mock_list_vehicles, mock_detect, mock_get_treasury, mock_get_rp_mode):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        mock_list_vehicles.return_value = {
            "123": {"isLastVehicle": True, "index": 0, "parts": [{"Key": "Damper_200"}]}
        }
        mock_detect.return_value = [{"key": "Damper_200", "slot": "Damper"}]

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )
        await DeliveryPoint.objects.acreate(guid="1", name="mine", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="2", name="factory", coord=Point(1000, 1000, 0)
        )

        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "Cargos": [
                    {
                        "Net_CargoKey": "Money",
                        "Net_Payment": 10_000,
                        "Net_Weight": 100.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 1000, "Y": 1000, "Z": 0},
                    }
                ],
                "PlayerId": str(player.unique_id),
                "CharacterGuid": str(character.guid),
            },
        }
        http_client_mod = MagicMock()
        await process_event(event, player, character, http_client_mod=http_client_mod)

        # It should call transfer_money to deduct the penalty
        mock_transfer.assert_called_once_with(
            http_client_mod,
            -10_000,
            "Modded Vehicle Penalty",
            str(player.unique_id),
        )
        mock_show_popup.assert_called_once()
        self.assertIn("profits were zeroed out", mock_show_popup.call_args[0][1])

    @patch("amc.webhook.detect_custom_parts")
    @patch("amc.webhook.list_player_vehicles", new_callable=AsyncMock)
    @patch("amc.webhook.show_popup", new_callable=AsyncMock)
    async def test_load_cargo_money_modded(self, mock_show_popup, mock_list_vehicles, mock_detect, mock_get_treasury, mock_get_rp_mode):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        
        mock_list_vehicles.return_value = {
            "123": {"isLastVehicle": True, "index": 0, "parts": [{"Key": "Damper_200"}]}
        }
        mock_detect.return_value = [{"key": "Damper_200", "slot": "Damper"}]

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)

        event = {
            "hook": "ServerLoadCargo",
            "timestamp": int(time.time()),
            "data": {
                "Cargo": {
                    "Net_CargoKey": "Money",
                    "Net_Payment": 10_000,
                },
                "PlayerId": str(player.unique_id),
                "CharacterGuid": str(character.guid),
            },
        }
        http_client_mod = MagicMock()
        await process_event(event, player, character, http_client_mod=http_client_mod)

        mock_show_popup.assert_called_once()
        self.assertIn("not allowed to use modified vehicles", mock_show_popup.call_args[0][1])

    @patch("amc.webhook.SMUGGLING_TIPOFF_ENABLED", True)
    @patch("amc.webhook._announce_smuggling_tipoff_after_delay", new_callable=AsyncMock)
    @patch("amc.webhook.detect_custom_parts")
    @patch("amc.webhook.list_player_vehicles", new_callable=AsyncMock)
    async def test_load_cargo_money_smuggling_tipoff(
        self, mock_list_vehicles, mock_detect, mock_announce_tipoff, mock_get_treasury, mock_get_rp_mode,
    ):
        """First Money load triggers a smuggling tip-off announcement."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        mock_list_vehicles.return_value = {}
        mock_detect.return_value = []

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)

        event = {
            "hook": "ServerLoadCargo",
            "timestamp": int(time.time()),
            "data": {
                "Cargo": {"Net_CargoKey": "Money", "Net_Payment": 10_000},
                "PlayerId": str(player.unique_id),
                "CharacterGuid": str(character.guid),
            },
        }
        http_client = MagicMock()
        http_client_mod = MagicMock()

        await process_event(
            event, player, character,
            http_client=http_client, http_client_mod=http_client_mod,
        )
        # Let background task run
        await asyncio.sleep(0)

        mock_announce_tipoff.assert_called_once_with(http_client, delay=15)

    @patch("amc.webhook.SMUGGLING_TIPOFF_ENABLED", True)
    @patch("amc.webhook._announce_smuggling_tipoff_after_delay", new_callable=AsyncMock)
    @patch("amc.webhook.detect_custom_parts")
    @patch("amc.webhook.list_player_vehicles", new_callable=AsyncMock)
    async def test_load_cargo_money_smuggling_tipoff_throttled(
        self, mock_list_vehicles, mock_detect, mock_announce_tipoff, mock_get_treasury, mock_get_rp_mode,
    ):
        """Second Money load within 60s cooldown does NOT trigger another tip-off."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        mock_list_vehicles.return_value = {}
        mock_detect.return_value = []

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)

        event = {
            "hook": "ServerLoadCargo",
            "timestamp": int(time.time()),
            "data": {
                "Cargo": {"Net_CargoKey": "Money", "Net_Payment": 10_000},
                "PlayerId": str(player.unique_id),
                "CharacterGuid": str(character.guid),
            },
        }
        http_client = MagicMock()
        http_client_mod = MagicMock()

        # First load — triggers tip-off
        await process_event(
            event, player, character,
            http_client=http_client, http_client_mod=http_client_mod,
        )
        await asyncio.sleep(0)
        self.assertEqual(mock_announce_tipoff.call_count, 1)

        mock_announce_tipoff.reset_mock()

        # Second load — should be throttled (no new announcement)
        await process_event(
            event, player, character,
            http_client=http_client, http_client_mod=http_client_mod,
        )
        await asyncio.sleep(0)

        mock_announce_tipoff.assert_not_called()


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
class ProcessEventsTests(TestCase):
    def setUp(self):
        cache.clear()

    async def test_process_events_integration(
        self, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player1 = await sync_to_async(PlayerFactory)()
        character1 = await sync_to_async(CharacterFactory)(player=player1, guid="char1")
        await CharacterLocation.objects.acreate(
            character=character1, location=Point(0, 0, 0), vehicle_key="TestVehicle1"
        )
        player2 = await sync_to_async(PlayerFactory)()
        character2 = await sync_to_async(CharacterFactory)(player=player2, guid="char2")
        await CharacterLocation.objects.acreate(
            character=character2, location=Point(0, 0, 0), vehicle_key="TestVehicle2"
        )

        # Mocks for clients
        http_client = AsyncMock()
        http_client_mod = MagicMock()

        # Configure post to return an async context manager
        post_context = AsyncMock()
        post_context.__aenter__.return_value = MagicMock(status=200)
        post_context.__aexit__.return_value = None
        http_client_mod.post.return_value = post_context

        # Ensure get also works if needed (though get_rp_mode is patched)
        get_context = AsyncMock()
        get_context.__aenter__.return_value = MagicMock(status=200)
        get_context.__aexit__.return_value = None
        http_client_mod.get.return_value = get_context
        discord_client = AsyncMock()

        events = [
            {
                "hook": "ServerCargoArrived",
                "timestamp": int(time.time()),
                "data": {
                    "Cargos": [
                        {
                            "Net_CargoKey": "oranges",
                            "Net_Payment": 10000,
                            "Net_Weight": 100.0,
                            "Net_Damage": 0.0,
                            "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                            "Net_DestinationLocation": {"X": 1000, "Y": 1000, "Z": 0},
                        }
                    ],
                    "CharacterGuid": str(character1.guid),
                },
            },
            {
                "hook": "ServerCargoArrived",
                "timestamp": int(time.time()),
                "data": {
                    "Cargos": [
                        {
                            "Net_CargoKey": "oranges",
                            "Net_Payment": 10000,
                            "Net_Weight": 100.0,
                            "Net_Damage": 0.0,
                            "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                            "Net_DestinationLocation": {"X": 1000, "Y": 1000, "Z": 0},
                        }
                    ],
                    "CharacterGuid": str(character1.guid),
                },
            },
            {
                "hook": "ServerCargoArrived",
                "timestamp": int(time.time()),
                "data": {
                    "Cargos": [
                        {
                            "Net_CargoKey": "oranges",
                            "Net_Payment": 10000,
                            "Net_Weight": 100.0,
                            "Net_Damage": 0.0,
                            "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                            "Net_DestinationLocation": {"X": 1000, "Y": 1000, "Z": 0},
                        }
                    ],
                    "CharacterGuid": str(character2.guid),
                },
            },
        ]

        await DeliveryPoint.objects.acreate(guid="1", name="mine", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="2", name="factory", coord=Point(1000, 1000, 0)
        )

        await process_events(events, http_client, http_client_mod, discord_client)

        self.assertEqual(await ServerCargoArrivedLog.objects.acount(), 3)

        mock_jobs_cog = MagicMock()
        mock_jobs_cog.post_delivery_embed = AsyncMock()
        discord_client.get_cog.return_value = mock_jobs_cog

        await process_events(events[:1], http_client, http_client_mod, discord_client)


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
@patch("amc.webhook.announce", new_callable=AsyncMock)
@patch("amc.webhook.show_popup", new_callable=AsyncMock)
class ExtraWebhookTests(TestCase):
    def setUp(self):
        cache.clear()

    async def test_cargo_aggregation_same_event(
        self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode
    ):
        # Test that multiple cargos in one event are aggregated correctly
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="test-char-1"
        )
        from amc.models import PlayerStatusLog

        await PlayerStatusLog.objects.acreate(
            character=character,
            timespan=(timezone.now() - timedelta(minutes=5), timezone.now()),
        )
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="d1", name="D1", coord=Point(100, 100, 0)
        )

        events = [
            {
                "hook": "ServerCargoArrived",
                "timestamp": int(time.time()),
                "data": {
                    "CharacterGuid": str(character.guid),
                    "Cargos": [
                        {
                            "Net_CargoKey": "apples",
                            "Net_Payment": 1000,
                            "Net_Weight": 10.0,
                            "Net_Damage": 0.0,
                            "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                            "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                        },
                        {
                            "Net_CargoKey": "apples",
                            "Net_Payment": 1000,
                            "Net_Weight": 10.0,
                            "Net_Damage": 0.0,
                            "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                            "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                        },
                    ],
                },
            }
        ]

        await process_events(events)

        # Should have 2 logs in ServerCargoArrivedLog but only 1 Delivery record (aggregated)
        self.assertEqual(await ServerCargoArrivedLog.objects.acount(), 2)
        self.assertEqual(await Delivery.objects.filter(character=character).acount(), 1)
        delivery = await Delivery.objects.afirst()
        self.assertIsNotNone(delivery)
        self.assertEqual(delivery.quantity, 2)
        self.assertEqual(delivery.payment, 2000)

    async def test_shortcut_usage(
        self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode
    ):
        # Test that subsidy is zeroed if shortcut_zone_entered_at is recent
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="test-char-shortcut",
            shortcut_zone_entered_at=timezone.now(),
        )
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="d1", name="D1", coord=Point(100, 100, 0)
        )

        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {
                        "Net_CargoKey": "apples",
                        "Net_Payment": 1000,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }

        # Test via process_events to verify the timestamp-based zeroing
        await PlayerStatusLog.objects.acreate(
            character=character,
            timespan=(timezone.now() - timedelta(minutes=5), timezone.now()),
        )

        player_profits = []
        with patch(
            "amc.webhook.on_player_profits", new_callable=AsyncMock
        ) as mock_profits:
            await process_events([event], http_client_mod=MagicMock())
            await asyncio.sleep(0)
            player_profits = mock_profits.call_args[0][0]

        # player_profits format: (character, total_subsidy, total_payment, contract_payment)
        char, total_subsidy, total_payment, _ = player_profits[0]
        self.assertEqual(total_subsidy, 0)

        # Timestamp should be cleared after consumption
        await character.arefresh_from_db()
        self.assertIsNone(character.shortcut_zone_entered_at)

    async def test_cargo_dumped(
        self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="test-char-dumped"
        )
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        from amc.models import PlayerStatusLog

        await PlayerStatusLog.objects.acreate(
            character=character,
            timespan=(timezone.now() - timedelta(minutes=5), timezone.now()),
        )

        event = {
            "hook": "ServerCargoDumped",
            "timestamp": int(time.time()),
            "data": {
                "PlayerId": str(player.unique_id),
                "Cargo": {
                    "Net_CargoKey": "trash",
                    "Net_Payment": 500,
                    "Net_Weight": 50.0,
                    "Net_Damage": 0.1,
                },
            },
        }

        await process_events([event])

        self.assertEqual(await ServerCargoArrivedLog.objects.acount(), 1)
        log = await ServerCargoArrivedLog.objects.afirst()
        self.assertIsNotNone(log)
        self.assertEqual(log.cargo_key, "trash")
        self.assertEqual(log.payment, 500)

    async def test_vehicle_reset_rp_mode(
        self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = True
        player = await sync_to_async(PlayerFactory)()
        # Set last_login far enough in the past
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="test-char-rp"
        )
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )
        from amc.models import PlayerStatusLog

        await PlayerStatusLog.objects.acreate(
            character=character,
            timespan=(timezone.now() - timedelta(minutes=5), timezone.now()),
        )

        event = {
            "hook": "ServerResetVehicleAt",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
            },
        }

        await process_events([event])
        mock_announce.assert_called()
        self.assertIn("despawned", mock_announce.call_args[0][0])

    async def test_rp_mode_subsidy_fix(
        self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = True
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="test-char-rp-fix"
        )
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="d1", name="D1", coord=Point(100, 100, 0)
        )

        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {
                        "Net_CargoKey": "apples",
                        "Net_Payment": 1000,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    },
                    {
                        "Net_CargoKey": "oranges",
                        "Net_Payment": 1000,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    },
                ],
            },
        }

        await process_events([event], http_client=MagicMock())

        deliveries = [
            d async for d in Delivery.objects.filter(character=character).order_by("id")
        ]
        self.assertEqual(len(deliveries), 2)

        d1 = deliveries[0]
        d2 = deliveries[1]

        self.assertIsNotNone(d1)
        self.assertIsNotNone(d2)
        self.assertAlmostEqual(d1.subsidy, d2.subsidy, delta=1.0)

    @patch("amc.jobs.send_fund_to_player", new_callable=AsyncMock)
    async def test_proportional_job_rewards(
        self,
        mock_send_fund,
        mock_show_popup,
        mock_announce,
        mock_get_treasury,
        mock_get_rp_mode,
    ):
        from amc.webhook import on_delivery_job_fulfilled

        p1 = await sync_to_async(PlayerFactory)()
        c1 = await sync_to_async(CharacterFactory)(player=p1, name="Alice")
        p2 = await sync_to_async(PlayerFactory)()
        c2 = await sync_to_async(CharacterFactory)(player=p2, name="Bob")

        job = await DeliveryJob.objects.acreate(
            name="Community Goal",
            quantity_requested=10,
            quantity_fulfilled=10,
            completion_bonus=10000,
            bonus_multiplier=1.0,  # Added missing field
            expired_at=timezone.now() + timedelta(days=1),
        )

        # Alice delivered 7, Bob delivered 3
        await Delivery.objects.acreate(
            character=c1,
            job=job,
            quantity=7,
            timestamp=timezone.now(),
            payment=0,
            subsidy=0,
        )
        await Delivery.objects.acreate(
            character=c2,
            job=job,
            quantity=3,
            timestamp=timezone.now(),
            payment=0,
            subsidy=0,
        )

        await on_delivery_job_fulfilled(job, MagicMock())

        # Check Alice reward: 7/10 * 10000 = 7000
        # Check Bob reward: 3/10 * 10000 = 3000

        # send_fund_to_player(reward, character_obj, "Job Completion")
        fund_calls = mock_send_fund.call_args_list
        results = {call[0][1].id: call[0][0] for call in fund_calls}

        self.assertEqual(results[c1.id], 7000)
        self.assertEqual(results[c2.id], 3000)

    async def test_missing_character_skip(
        self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode
    ):
        # Test that events for non-existent characters are skipped
        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": "non-existent-guid",
                "PlayerId": "9999999",
                "Cargos": [],
            },
        }

        # Should not raise exception
        await process_events([event])
        self.assertEqual(await ServerCargoArrivedLog.objects.acount(), 0)

    async def test_process_event_exception_triggers_popup(
        self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode
    ):
        # Test that an exception in process_event triggers a popup
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="test-char-exception"
        )
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        from amc.models import PlayerStatusLog

        await PlayerStatusLog.objects.acreate(
            character=character,
            timespan=(timezone.now() - timedelta(minutes=5), timezone.now()),
        )

        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {}
                ],  # This will cause an error in process_cargo_log (missing keys)
            },
        }

        with self.assertRaises(Exception):
            await process_events([event], http_client_mod=MagicMock())

        # Yield to allow the background task (show_popup) to run
        await asyncio.sleep(0.1)

        mock_show_popup.assert_called()
        self.assertIn("Webhook failed", mock_show_popup.call_args[0][1])

    @patch("amc.webhook.on_delivery_job_fulfilled")
    async def test_over_delivery_job_completion(
        self,
        mock_on_fulfilled,
        mock_show_popup,
        mock_announce,
        mock_get_treasury,
        mock_get_rp_mode,
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="test-char-over"
        )
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        p1 = await DeliveryPoint.objects.acreate(
            guid="s_over", name="S_Over", coord=Point(0, 0, 0)
        )
        p2 = await DeliveryPoint.objects.acreate(
            guid="d_over", name="D_Over", coord=Point(100, 100, 0)
        )

        # 1. Create a job with 29/30 fulfilled
        job = await DeliveryJob.objects.acreate(
            name="Over Delivery Job",
            # Cargo key must match the event
            cargo_key="apples",
            quantity_requested=30,
            quantity_fulfilled=29,
            completion_bonus=10000,
            bonus_multiplier=1.0,
            expired_at=timezone.now() + timedelta(days=1),
        )
        await job.source_points.aadd(p1)
        await job.destination_points.aadd(p2)

        # 2. Create dummy delivery for the 29 items so on_delivery_job_fulfilled can find them
        # (Though we are mocking on_delivery_job_fulfilled, so strictly speaking we verify it is CALLED.
        # But for correctness of the system state, let's create them)
        await Delivery.objects.acreate(
            character=character,
            job=job,
            quantity=29,
            timestamp=timezone.now(),
            payment=0,
            subsidy=0,
            cargo_key="apples",
            sender_point=p1,
            destination_point=p2,
        )

        # 3. Simulate existing delivery filling it to 29?
        # Actually the job.quantity_fulfilled is 29. The Delivery rows support it. All good.

        # 4. Arrive with 10 more items
        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {"CharacterGuid": str(character.guid), "Cargos": []},
        }
        # Create 10 cargos
        cargos: list[dict[str, Any]] = list(event["data"]["Cargos"])  # type: ignore
        for _ in range(10):
            cargos.append(
                {
                    "Net_CargoKey": "apples",
                    "Net_Payment": 100,
                    "Net_Weight": 10.0,
                    "Net_Damage": 0.0,
                    "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                    "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                }
            )
        event["data"]["Cargos"] = cargos

        await process_events([event], http_client=MagicMock())

        # 5. Verify logic
        await job.arefresh_from_db()
        self.assertEqual(
            job.quantity_fulfilled, 30, "Job should be capped at 30 fulfilled"
        )

        # Verify on_delivery_job_fulfilled called exactly once
        self.assertEqual(mock_on_fulfilled.call_count, 1)

    @patch("amc.webhook.on_delivery_job_fulfilled")
    async def test_multi_job_completion(
        self,
        mock_on_fulfilled,
        mock_show_popup,
        mock_announce,
        mock_get_treasury,
        mock_get_rp_mode,
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="test-char-multi"
        )
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        p1 = await DeliveryPoint.objects.acreate(
            guid="s_multi", name="S_Multi", coord=Point(0, 0, 0)
        )
        p2 = await DeliveryPoint.objects.acreate(
            guid="d_multi", name="D_Multi", coord=Point(100, 100, 0)
        )

        # Job A: Apples
        job_a = await DeliveryJob.objects.acreate(
            name="Job A",
            cargo_key="apples",
            quantity_requested=10,
            quantity_fulfilled=9,
            completion_bonus=5000,
            bonus_multiplier=1.0,
            expired_at=timezone.now() + timedelta(days=1),
        )
        await job_a.source_points.aadd(p1)
        await job_a.destination_points.aadd(p2)

        # Job B: Oranges
        job_b = await DeliveryJob.objects.acreate(
            name="Job B",
            cargo_key="oranges",
            quantity_requested=10,
            quantity_fulfilled=9,
            completion_bonus=6000,
            bonus_multiplier=1.0,
            expired_at=timezone.now() + timedelta(days=1),
        )
        await job_b.source_points.aadd(p1)
        await job_b.destination_points.aadd(p2)

        # Event with 1 Apple and 1 Orange
        # Both should complete their respective jobs.
        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {
                        "Net_CargoKey": "apples",
                        "Net_Payment": 100,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    },
                    {
                        "Net_CargoKey": "oranges",
                        "Net_Payment": 100,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    },
                ],
            },
        }

        await process_events([event], http_client=MagicMock())

        await job_a.arefresh_from_db()
        await job_b.arefresh_from_db()

        self.assertEqual(job_a.quantity_fulfilled, 10, "Job A should be fulfilled")
        self.assertEqual(job_b.quantity_fulfilled, 10, "Job B should be fulfilled")

        # Verify on_delivery_job_fulfilled called TWICE (once for each job)
        # Note: on_delivery_job_fulfilled(job, client)
        self.assertEqual(
            mock_on_fulfilled.call_count, 2, "Both jobs should trigger completion hook"
        )

        # Optional: check args to ensure both jobs were passed
        called_jobs = {call.args[0].id for call in mock_on_fulfilled.call_args_list}
        self.assertEqual(called_jobs, {job_a.id, job_b.id})

    @patch("amc.jobs.send_fund_to_player", new_callable=AsyncMock)
    async def test_job_completion_rewards_integration(
        self,
        mock_send_fund,
        mock_show_popup,
        mock_announce,
        mock_get_treasury,
        mock_get_rp_mode,
    ):
        # This test DOES NOT mock on_delivery_job_fulfilled.
        # It verifies that the whole flow results in money being sent.
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="test-char-reward"
        )
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        p1 = await DeliveryPoint.objects.acreate(
            guid="s_rew", name="S_Rew", coord=Point(0, 0, 0)
        )
        p2 = await DeliveryPoint.objects.acreate(
            guid="d_rew", name="D_Rew", coord=Point(100, 100, 0)
        )

        job = await DeliveryJob.objects.acreate(
            name="Reward Job",
            cargo_key="gold",
            quantity_requested=10,
            quantity_fulfilled=9,
            completion_bonus=10000,
            bonus_multiplier=1.0,
            expired_at=timezone.now() + timedelta(days=1),
        )
        await job.source_points.aadd(p1)
        await job.destination_points.aadd(p2)

        # Helper to create existing delivery
        await Delivery.objects.acreate(
            character=character,
            job=job,
            quantity=9,
            timestamp=timezone.now(),
            payment=0,
            subsidy=0,
            cargo_key="gold",
            sender_point=p1,
            destination_point=p2,
        )

        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {
                        "Net_CargoKey": "gold",
                        "Net_Payment": 100,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }

        # We need to allow on_delivery_job_fulfilled to actually run, so we need to ensure it's imported
        # and NOT patched in this specific test method (or rather, we test the side effect: send_fund_to_player).
        # However, the CLASS might have patches.
        # The class ExtraWebhookTests has patches on: get_rp_mode, get_treasury, announce, show_popup.
        # It DOES NOT patch on_delivery_job_fulfilled.

        await process_events([event], http_client=MagicMock())

        # Wait for background tasks
        await asyncio.sleep(0.1)

        await job.arefresh_from_db()
        self.assertIsNotNone(job.fulfilled_at)

        # Verify money was sent.
        # Total contribution: 9 (existing) + 1 (new) = 10.
        # Reward = 10000.
        # Alice (character) should get 10000.

        self.assertEqual(mock_send_fund.call_count, 1, "Should send funds exactly once")
        self.assertEqual(
            mock_send_fund.call_args[0][0], 10000, "Should send 10000 reward"
        )


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
@patch("amc.webhook.announce", new_callable=AsyncMock)
@patch("amc.webhook.show_popup", new_callable=AsyncMock)
class SubsidyIntegrationTests(TestCase):
    def setUp(self):
        cache.clear()

    async def test_subsidy_rule_integration(
        self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="test-char-subsidy"
        )
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        # Create Cargo
        cargo_apple, _ = await Cargo.objects.aget_or_create(
            key="apples", defaults={"label": "Apples"}
        )

        # Create Subsidy Rule
        rule = await SubsidyRule.objects.acreate(
            name="Apple Subsidy",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("2.0"),  # 200%
            active=True,
            priority=10,
            allocation=Decimal("100000"),
            spent=Decimal("0"),
        )
        await rule.cargos.aadd(cargo_apple)

        # Delivery Points
        await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="d1", name="D1", coord=Point(100, 100, 0)
        )

        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {
                        "Net_CargoKey": "apples",
                        "Net_Payment": 1000,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }

        # Run process_events
        await process_events([event], http_client=MagicMock())

        # Verify Delivery
        delivery = await Delivery.objects.filter(character=character).afirst()
        self.assertIsNotNone(delivery, "Delivery should be created")

        # Expected subsidy: 1000 * 2.0 = 2000
        self.assertEqual(
            delivery.subsidy,
            2000,
            "Subsidy should be calculated correctly based on Rule",
        )

        # Verify Rule Spent updated
        await rule.arefresh_from_db()
        self.assertEqual(rule.spent, 2000, "SubsidyRule.spent should be updated")

    @unittest.skip("Allocation limit logic temporarily disabled")
    async def test_subsidy_allocation_limit_integration(
        self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode
    ):
        # Test that subsidy is capped by allocation
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="test-char-capped"
        )
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        cargo_gold, _ = await Cargo.objects.aget_or_create(
            key="gold", defaults={"label": "Gold"}
        )

        # Rule with only 500 remaining allocation
        # allocation=2000, spent=1500 -> remaining=500
        rule = await SubsidyRule.objects.acreate(
            name="Gold Subsidy",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("1.0"),  # 100%
            active=True,
            priority=10,
            allocation=Decimal("2000"),
            spent=Decimal("1500"),
        )
        await rule.cargos.aadd(cargo_gold)

        await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="d1", name="D1", coord=Point(100, 100, 0)
        )

        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {
                        "Net_CargoKey": "gold",
                        "Net_Payment": 1000,  # with 100% subsidy should be 1000 subsidy
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }

        await process_events([event], http_client=MagicMock())

        delivery = await Delivery.objects.filter(character=character).afirst()
        self.assertIsNotNone(delivery)

        # Subsidy should be capped at 500 (remaining allocation)
        self.assertEqual(
            delivery.subsidy, 500, "Subsidy should be capped by rule allocation"
        )

        await rule.arefresh_from_db()
        self.assertEqual(rule.spent, 2000, "Rule should now be fully spent")

    async def test_delivery_point_tolerance(
        self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="test-char-tolerance"
        )
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        cargo_coal, _ = await Cargo.objects.aget_or_create(
            key="coal", defaults={"label": "Coal"}
        )

        # Rule requires specific source point
        # Point is at (0,0)
        p_zero = await DeliveryPoint.objects.acreate(
            guid="p0", name="Zero Point", coord=Point(0, 0, 0)
        )

        rule = await SubsidyRule.objects.acreate(
            name="Proximity Rule",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("1.0"),
            active=True,
            priority=10,
            allocation=Decimal("100000"),
        )
        await rule.cargos.aadd(cargo_coal)
        await rule.source_delivery_points.aadd(p_zero)
        # Note: SubsidyRule checks filter(Q(source_delivery_points__coord__dwithin=(cargo.sender_point.coord, 1.0)))
        # AND Filtering for the Delivery Point itself (Webhook logic) uses buffer(1)

        # 1. Test EXACT Location -> Should Match
        base_ts = int(time.time())
        event_exact = {
            "hook": "ServerCargoArrived",
            "timestamp": base_ts,
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {
                        "Net_CargoKey": "coal",
                        "Net_Payment": 100,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }
        await process_events([event_exact], http_client=MagicMock())
        deliveries = [
            d
            async for d in Delivery.objects.filter(character=character).order_by("-id")
        ]
        d1 = deliveries[0]
        self.assertIsNotNone(d1)
        self.assertEqual(d1.subsidy, 100, "Exact match should get subsidy")

        # 2. Test Within Tolerance (0.9) -> Should Match
        # But wait! Webhook 'process_cargo_log' logic:
        # sender_coord = Point(X,Y,Z).buffer(1)
        # sender = DeliveryPoint.objects.filter(coord__coveredby=sender_coord).afirst()
        # If I send (0.9, 0, 0), Point(0.9,0,0).buffer(1) creates circle from -0.1 to 1.9.
        # Zero point (0,0,0) IS covered by that circle.
        # So "sender_point" on the Application Log will be resolved essentially?
        # YES. If resolved, then SubsidyRule uses `cargo.sender_point` (which is the DB object).
        # So if webhook resolves it, SubsidyRule sees the DB object.

        event_near = {
            "hook": "ServerCargoArrived",
            "timestamp": base_ts + 1,
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {
                        "Net_CargoKey": "coal",
                        "Net_Payment": 100,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0.9, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }
        await process_events([event_near], http_client=MagicMock())
        d2 = await Delivery.objects.filter(character=character).order_by("-id").afirst()
        self.assertIsNotNone(d2)
        self.assertEqual(
            d2.subsidy, 100, "0.9 distance should resolve point and get subsidy"
        )

        # 3. Test Outside Tolerance (2.1) -> Should NOT Match
        # Point(2.1, 0, 0).buffer(1) -> Circle from 1.1 to 3.1.
        # Zero point (0,0,0) is clearly NOT covered (distance 2.1 >> radius 1).
        # So 'sender_point' will be None.
        # SubsidyRule loop: if cargo.sender_point is None:
        #   rules = rules.filter(source_areas__isnull=True, source_delivery_points__isnull=True)
        # Our rule HAS source_delivery_points, so it should be filtered out.

        event_far = {
            "hook": "ServerCargoArrived",
            "timestamp": base_ts + 2,
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {
                        "Net_CargoKey": "coal",
                        "Net_Payment": 100,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 2.1, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }
        await process_events([event_far], http_client=MagicMock())
        d3 = await Delivery.objects.filter(character=character).order_by("-id").afirst()
        self.assertIsNotNone(d3)
        self.assertEqual(
            d3.subsidy,
            0,
            "2.1 distance should NOT resolve point and thus NOT get subsidy",
        )

    async def test_subsidy_zero_treasury(
        self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode
    ):
        # Verify that 0 treasury results in 0 subsidy
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 0

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="test-char-zero-treasury"
        )
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        cargo, _ = await Cargo.objects.aget_or_create(
            key="coal", defaults={"label": "Coal"}
        )
        p_start = await DeliveryPoint.objects.acreate(
            guid="pS", name="Start", coord=Point(0, 0, 0)
        )

        rule = await SubsidyRule.objects.acreate(
            name="Treasury Check",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("1.0"),
            active=True,
            priority=10,
            allocation=Decimal("100000"),
        )
        await rule.cargos.aadd(cargo)
        await rule.source_delivery_points.aadd(p_start)

        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {
                        "Net_CargoKey": "coal",
                        "Net_Payment": 1000,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {
                            "X": 0,
                            "Y": 0,
                            "Z": 0,
                        },  # Matches p_start
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }
        await process_events([event], http_client=MagicMock())
        d = await Delivery.objects.filter(character=character).afirst()
        self.assertIsNotNone(d)
        self.assertEqual(d.subsidy, 0, "Zero treasury should result in zero subsidy")

    async def test_subsidy_cargo_case_mismatch(
        self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode
    ):
        # Verify if 'Coal' vs 'coal' mismatch causes 0 subsidy
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="test-char-case"
        )
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )

        # Rule uses 'Coal' (Title Case)
        cargo, _ = await Cargo.objects.aget_or_create(
            key="Coal", defaults={"label": "Coal"}
        )

        rule = await SubsidyRule.objects.acreate(
            name="Case Sensitive Rule",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("1.0"),
            active=True,
            priority=10,
            allocation=Decimal("100000"),
        )
        await rule.cargos.aadd(cargo)

        # Event uses 'coal' (lower case)
        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {
                        "Net_CargoKey": "coal",
                        "Net_Payment": 1000,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }
        await process_events([event], http_client=MagicMock())
        d = await Delivery.objects.filter(character=character).afirst()

        # If strict matching, this should be 0.
        # Note: In Python, string equality is case sensitive. In Postgres, defaults are too.
        # This test ensures we KNOW if it's failing due to case.
        self.assertIsNotNone(d)
        self.assertEqual(
            d.subsidy,
            0,
            "Case mismatch should result in zero subsidy (if strictly matched)",
        )


class OnPlayerProfitTests(TestCase):
    @patch("amc.webhook.set_aside_player_savings", new_callable=AsyncMock)
    @patch("amc.webhook.repay_loan_for_profit", new_callable=AsyncMock)
    @patch("amc.webhook.subsidise_player", new_callable=AsyncMock)
    async def test_contract_payment_included_in_actual_income(
        self, mock_subsidise, mock_repay_loan, mock_savings
    ):
        """Contract payment should be included in actual_income for non-gov-employees."""
        from amc.webhook import on_player_profit

        mock_repay_loan.return_value = 0

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, reject_ubi=False
        )

        session = MagicMock()
        total_subsidy = 0
        total_payment = 10_000  # base cargo earnings
        contract_payment = 50_000  # contract completion payment

        await on_player_profit(
            character, total_subsidy, total_payment, session,
            contract_payment=contract_payment,
        )

        # set_aside_player_savings should receive actual_income = 10000 + 50000 = 60000
        mock_savings.assert_called_once()
        savings_amount = mock_savings.call_args[0][1]
        self.assertEqual(savings_amount, 60_000)

    @patch("amc.webhook.set_aside_player_savings", new_callable=AsyncMock)
    @patch("amc.webhook.repay_loan_for_profit", new_callable=AsyncMock)
    @patch("amc.webhook.subsidise_player", new_callable=AsyncMock)
    async def test_contract_payment_with_reject_ubi(
        self, mock_subsidise, mock_repay_loan, mock_savings
    ):
        """Contract payment should still be deposited even with reject_ubi=True."""
        from amc.webhook import on_player_profit

        mock_repay_loan.return_value = 0

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, reject_ubi=True
        )

        session = MagicMock()
        # total_subsidy = 5000, base_payment = 10000 (subsidy NOT baked in), contract = 50000
        total_subsidy = 5_000
        base_payment = 10_000
        contract_payment = 50_000

        await on_player_profit(
            character, total_subsidy, base_payment, session,
            contract_payment=contract_payment,
        )

        # reject_ubi zeroes subsidy, so actual_income = 10000 + 0 + 50000 = 60000
        mock_subsidise.assert_not_called()
        mock_savings.assert_called_once()
        savings_amount = mock_savings.call_args[0][1]
        self.assertEqual(savings_amount, 60_000)

    @patch("amc.webhook.set_aside_player_savings", new_callable=AsyncMock)
    @patch("amc.webhook.repay_loan_for_profit", new_callable=AsyncMock)
    @patch("amc.webhook.subsidise_player", new_callable=AsyncMock)
    async def test_depot_restock_subsidy_only(
        self, mock_subsidise, mock_repay_loan, mock_savings
    ):
        """Depot restock: base_payment=0, subsidy=10_000.

        The game does NOT deposit money for depot restocking — the 10,000
        is purely a system subsidy. Passing subsidy_amount as both subsidy
        AND base_payment would double-count the income.
        """
        from amc.webhook import on_player_profit

        mock_repay_loan.return_value = 5_000  # loan takes 5k

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, reject_ubi=False
        )

        session = MagicMock()
        subsidy_amount = 10_000

        # This mirrors the fixed call in tasks.py: base_payment=0
        await on_player_profit(
            character, subsidy_amount, 0, session,
        )

        # Subsidy of 10_000 should be paid to wallet
        mock_subsidise.assert_called_once_with(subsidy_amount, character, session)

        # Loan repayment should be called with actual_income=10_000 (NOT 20_000)
        mock_repay_loan.assert_called_once_with(character, 10_000, session)

        # Savings = actual_income - repayment = 10_000 - 5_000 = 5_000
        mock_savings.assert_called_once()
        savings_amount = mock_savings.call_args[0][1]
        self.assertEqual(savings_amount, 5_000)

    @patch("amc.webhook.set_aside_player_savings", new_callable=AsyncMock)
    @patch("amc.webhook.repay_loan_for_profit", new_callable=AsyncMock)
    @patch("amc.webhook.subsidise_player", new_callable=AsyncMock)
    async def test_depot_restock_double_count_regression(
        self, mock_subsidise, mock_repay_loan, mock_savings
    ):
        """Regression: passing subsidy as base_payment MUST NOT happen.

        If base_payment=10_000, actual_income becomes 20_000 and the system
        deducts repayment + savings based on 20_000 — but only 10_000 was
        ever deposited to the wallet (the subsidy). This would drain the
        player's pre-existing wallet balance.
        """
        from amc.webhook import on_player_profit

        mock_repay_loan.return_value = 10_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, reject_ubi=False
        )

        session = MagicMock()
        subsidy_amount = 10_000

        # Correct call: base_payment=0
        await on_player_profit(
            character, subsidy_amount, 0, session,
        )

        # actual_income should be 10_000 (subsidy only)
        repay_call_income = mock_repay_loan.call_args[0][1]
        self.assertEqual(
            repay_call_income, 10_000,
            "Loan repayment income should be subsidy only, not double-counted"
        )

    @patch("amc.webhook.transfer_money", new_callable=AsyncMock)
    @patch("amc.gov_employee.redirect_income_to_treasury", new_callable=AsyncMock)
    @patch("amc.webhook.set_aside_player_savings", new_callable=AsyncMock)
    @patch("amc.webhook.repay_loan_for_profit", new_callable=AsyncMock)
    @patch("amc.webhook.subsidise_player", new_callable=AsyncMock)
    async def test_gov_employee_subsidy_only_contribution(
        self, mock_subsidise, mock_repay_loan, mock_savings,
        mock_redirect, mock_transfer,
    ):
        """Gov employee with subsidy-only profit (depot restock).

        The subsidy should be sent to wallet, confiscated back, and recorded
        as a gov contribution — but NOT as a treasury donation (since the
        money came from the treasury in the first place).
        """
        from amc.webhook import on_player_profit

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, reject_ubi=False,
            gov_employee_until=timezone.now() + timedelta(hours=24),
        )

        session = MagicMock()
        await on_player_profit(character, 10_000, 0, session)

        # Subsidy should be paid to wallet
        mock_subsidise.assert_called_once_with(10_000, character, session)

        # Then confiscated back
        mock_transfer.assert_called_once_with(
            session, -10_000, "Government Service",
            str(character.player.unique_id),
        )

        # Contribution tracked with amount=0 (no donation) and contribution=10_000
        mock_redirect.assert_called_once_with(
            0, character, "Government Service – Subsidy",
            http_client=None, session=session, contribution=10_000,
        )

        # Non-gov paths should NOT be called
        mock_repay_loan.assert_not_called()
        mock_savings.assert_not_called()

    @patch("amc.webhook.transfer_money", new_callable=AsyncMock)
    @patch("amc.gov_employee.redirect_income_to_treasury", new_callable=AsyncMock)
    @patch("amc.webhook.set_aside_player_savings", new_callable=AsyncMock)
    @patch("amc.webhook.repay_loan_for_profit", new_callable=AsyncMock)
    @patch("amc.webhook.subsidise_player", new_callable=AsyncMock)
    async def test_gov_employee_base_plus_subsidy_contribution(
        self, mock_subsidise, mock_repay_loan, mock_savings,
        mock_redirect, mock_transfer,
    ):
        """Gov employee with both base_payment and subsidy.

        base_payment is confiscated and donated to treasury.
        subsidy is sent to wallet, confiscated, and counted as contribution only.
        """
        from amc.webhook import on_player_profit

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, reject_ubi=False,
            gov_employee_until=timezone.now() + timedelta(hours=24),
        )

        session = MagicMock()
        await on_player_profit(character, 5_000, 10_000, session)

        # Two transfer_money calls:
        # 1. -10_000 for base_payment confiscation
        # 2. -5_000 for subsidy confiscation
        calls = mock_transfer.call_args_list
        self.assertEqual(len(calls), 2)
        # Base confiscation
        self.assertEqual(calls[0].args[1], -10_000)
        # Subsidy confiscation
        self.assertEqual(calls[1].args[1], -5_000)

        # Two redirect_income_to_treasury calls:
        redirect_calls = mock_redirect.call_args_list
        self.assertEqual(len(redirect_calls), 2)
        # 1. Earnings: amount=10_000 (real money → treasury donation)
        self.assertEqual(redirect_calls[0].args[0], 10_000)
        self.assertEqual(redirect_calls[0].args[2], "Government Service – Earnings")
        # 2. Subsidy: amount=0 (no donation), contribution=5_000
        self.assertEqual(redirect_calls[1].args[0], 0)
        self.assertEqual(redirect_calls[1].args[2], "Government Service – Subsidy")
        self.assertEqual(redirect_calls[1].kwargs["contribution"], 5_000)

        # Subsidy paid to wallet
        mock_subsidise.assert_called_once_with(5_000, character, session)

        # Non-gov paths should NOT be called
        mock_repay_loan.assert_not_called()
        mock_savings.assert_not_called()


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
class SeqDeduplicationTests(TestCase):
    """Tests for _seq-based idempotency in process_events."""

    def setUp(self):
        cache.clear()

    def _make_cargo_event(self, character_guid, seq=None):
        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character_guid),
                "Cargos": [
                    {
                        "Net_CargoKey": "oranges",
                        "Net_Payment": 1000,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }
        if seq is not None:
            event["_seq"] = seq
        return event

    async def test_seq_dedup_skips_already_processed(
        self, mock_get_treasury, mock_get_rp_mode
    ):
        """Events with _seq <= last_processed should be skipped."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestV"
        )
        await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="d1", name="D1", coord=Point(100, 100, 0)
        )

        from django.core.cache import cache
        cache.set("webhook:last_processed_seq", 5, timeout=None)

        events = [
            self._make_cargo_event(character.guid, seq=3),  # skip
            self._make_cargo_event(character.guid, seq=5),  # skip (==)
            self._make_cargo_event(character.guid, seq=6),  # process
            self._make_cargo_event(character.guid, seq=7),  # process
        ]

        await process_events(events)

        self.assertEqual(await ServerCargoArrivedLog.objects.acount(), 2)

        # High-water mark should be updated to 7
        self.assertEqual(cache.get("webhook:last_processed_seq"), 7)

        cache.delete("webhook:last_processed_seq")

    async def test_seq_dedup_backward_compat_no_seq(
        self, mock_get_treasury, mock_get_rp_mode
    ):
        """Events without _seq should always pass through (old mod compat)."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestV"
        )
        await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="d1", name="D1", coord=Point(100, 100, 0)
        )

        from django.core.cache import cache
        cache.set("webhook:last_processed_seq", 99, timeout=None)

        # No _seq field — should not be filtered
        events = [
            self._make_cargo_event(character.guid),
            self._make_cargo_event(character.guid),
        ]

        await process_events(events)

        # Both events processed despite high-water mark
        self.assertEqual(await ServerCargoArrivedLog.objects.acount(), 2)

        # High-water mark unchanged (no _seq to update it)
        self.assertEqual(cache.get("webhook:last_processed_seq"), 99)

        cache.delete("webhook:last_processed_seq")

    async def test_seq_dedup_mixed_old_and_new(
        self, mock_get_treasury, mock_get_rp_mode
    ):
        """Mix of events with and without _seq: old events pass through, new events are filtered."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestV"
        )
        await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="d1", name="D1", coord=Point(100, 100, 0)
        )

        from django.core.cache import cache
        cache.set("webhook:last_processed_seq", 10, timeout=None)

        events = [
            self._make_cargo_event(character.guid, seq=8),   # skip (old)
            self._make_cargo_event(character.guid),           # pass (no _seq)
            self._make_cargo_event(character.guid, seq=11),  # process (new)
        ]

        await process_events(events)

        # 2 events processed: the no-seq one + seq=11
        self.assertEqual(await ServerCargoArrivedLog.objects.acount(), 2)

        # High-water mark updated to 11
        self.assertEqual(cache.get("webhook:last_processed_seq"), 11)

        cache.delete("webhook:last_processed_seq")

    async def test_epoch_change_resets_high_water_mark(
        self, mock_get_treasury, mock_get_rp_mode
    ):
        """Events with a new _epoch should reset the seq dedup, even if seq < old mark."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestV"
        )
        await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="d1", name="D1", coord=Point(100, 100, 0)
        )

        from django.core.cache import cache
        # Simulate: old server session had epoch=1000, high-water mark at seq=50
        cache.set("webhook:last_processed_seq", 50, timeout=None)
        cache.set("webhook:last_epoch", 1000, timeout=None)

        # New server starts with epoch=2000, seq starts from 1
        events = [
            {**self._make_cargo_event(character.guid, seq=1), "_epoch": 2000},
            {**self._make_cargo_event(character.guid, seq=2), "_epoch": 2000},
        ]

        await process_events(events)

        # Both events should be processed (epoch change resets mark)
        self.assertEqual(await ServerCargoArrivedLog.objects.acount(), 2)

        # High-water mark should now be 2 (from the new epoch)
        self.assertEqual(cache.get("webhook:last_processed_seq"), 2)
        # Epoch should be updated
        self.assertEqual(cache.get("webhook:last_epoch"), 2000)

        cache.delete("webhook:last_processed_seq")
        cache.delete("webhook:last_epoch")


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
@patch("amc.webhook.subsidise_player", new_callable=AsyncMock)
class SecurityBonusTests(TestCase):
    """Test Risk Premium on Money deliveries based on active online police."""

    def _make_money_event(self, player, character):
        return {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "Cargos": [
                    {
                        "Net_CargoKey": "Money",
                        "Net_Payment": 10_000,
                        "Net_Weight": 100.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 1000, "Y": 1000, "Z": 0},
                    }
                ],
                "PlayerId": str(player.unique_id),
                "CharacterGuid": str(character.guid),
            },
        }

    @patch("amc.webhook.list_player_vehicles", new_callable=AsyncMock)
    async def test_money_delivery_no_police_no_bonus(
        self, mock_list_vehicles, mock_subsidise, mock_get_treasury, mock_get_rp_mode
    ):
        """0 police on duty → 0% risk premium."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        mock_list_vehicles.return_value = {}

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )
        await DeliveryPoint.objects.acreate(guid="1", name="mine", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="2", name="factory", coord=Point(1000, 1000, 0)
        )

        event = self._make_money_event(player, character)
        http_client_mod = MagicMock()
        payment, subsidy, _ = await process_event(
            event, player, character, http_client_mod=http_client_mod
        )

        self.assertEqual(payment, 10_000)
        # No police → no risk premium; subsidise_player not called
        self.assertEqual(subsidy, 0)
        mock_subsidise.assert_not_called()

    @patch("amc.webhook.list_player_vehicles", new_callable=AsyncMock)
    async def test_money_delivery_one_police_20pct_bonus(
        self, mock_list_vehicles, mock_subsidise, mock_get_treasury, mock_get_rp_mode
    ):
        """1 police on duty and online → 20% risk premium."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        mock_list_vehicles.return_value = {}

        from amc.models import PoliceSession

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )
        await DeliveryPoint.objects.acreate(guid="1", name="mine", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="2", name="factory", coord=Point(1000, 1000, 0)
        )

        # Create a police officer who is on duty and online
        cop_player = await sync_to_async(PlayerFactory)()
        cop_char = await sync_to_async(CharacterFactory)(
            player=cop_player, last_online=timezone.now()
        )
        await PoliceSession.objects.acreate(character=cop_char)

        event = self._make_money_event(player, character)
        http_client_mod = MagicMock()
        payment, subsidy, _ = await process_event(
            event, player, character, http_client_mod=http_client_mod
        )

        self.assertEqual(payment, 10_000)
        # Risk premium paid directly, not in returned subsidy
        self.assertEqual(subsidy, 0)
        # 1 police * 20% * 10,000 = 2,000 risk premium
        mock_subsidise.assert_called_once_with(
            2_000, character, http_client_mod, message="Risk Premium"
        )

    @patch("amc.webhook.list_player_vehicles", new_callable=AsyncMock)
    async def test_money_delivery_offline_police_no_bonus(
        self, mock_list_vehicles, mock_subsidise, mock_get_treasury, mock_get_rp_mode
    ):
        """Police on duty but offline (stale last_online) → no risk premium."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        mock_list_vehicles.return_value = {}

        from amc.models import PoliceSession

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )
        await DeliveryPoint.objects.acreate(guid="1", name="mine", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="2", name="factory", coord=Point(1000, 1000, 0)
        )

        # Create a police officer on duty but last_online is 5 minutes ago (stale)
        cop_player = await sync_to_async(PlayerFactory)()
        cop_char = await sync_to_async(CharacterFactory)(
            player=cop_player, last_online=timezone.now() - timedelta(minutes=5)
        )
        await PoliceSession.objects.acreate(character=cop_char)

        event = self._make_money_event(player, character)
        http_client_mod = MagicMock()
        payment, subsidy, _ = await process_event(
            event, player, character, http_client_mod=http_client_mod
        )

        self.assertEqual(payment, 10_000)
        # Offline police → not counted → no risk premium
        self.assertEqual(subsidy, 0)
        mock_subsidise.assert_not_called()

    @patch("amc.webhook.list_player_vehicles", new_callable=AsyncMock)
    async def test_money_delivery_two_police_40pct_bonus(
        self, mock_list_vehicles, mock_subsidise, mock_get_treasury, mock_get_rp_mode
    ):
        """2 police on duty and online → 40% risk premium."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        mock_list_vehicles.return_value = {}

        from amc.models import PoliceSession

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )
        await DeliveryPoint.objects.acreate(guid="1", name="mine", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="2", name="factory", coord=Point(1000, 1000, 0)
        )

        now = timezone.now()
        for _ in range(2):
            cp = await sync_to_async(PlayerFactory)()
            cc = await sync_to_async(CharacterFactory)(player=cp, last_online=now)
            await PoliceSession.objects.acreate(character=cc)

        event = self._make_money_event(player, character)
        http_client_mod = MagicMock()
        payment, subsidy, _ = await process_event(
            event, player, character, http_client_mod=http_client_mod
        )

        self.assertEqual(payment, 10_000)
        # Risk premium paid directly, not in returned subsidy
        self.assertEqual(subsidy, 0)
        # 2 police * 20% * 10,000 = 4,000 risk premium
        mock_subsidise.assert_called_once_with(
            4_000, character, http_client_mod, message="Risk Premium"
        )

    @patch("amc.webhook.list_player_vehicles", new_callable=AsyncMock)
    async def test_money_delivery_bonus_capped_at_100pct(
        self, mock_list_vehicles, mock_subsidise, mock_get_treasury, mock_get_rp_mode
    ):
        """6 police → would be 120%, but capped at 100%."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        mock_list_vehicles.return_value = {}

        from amc.models import PoliceSession

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )
        await DeliveryPoint.objects.acreate(guid="1", name="mine", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="2", name="factory", coord=Point(1000, 1000, 0)
        )

        now = timezone.now()
        for _ in range(6):
            cp = await sync_to_async(PlayerFactory)()
            cc = await sync_to_async(CharacterFactory)(player=cp, last_online=now)
            await PoliceSession.objects.acreate(character=cc)

        event = self._make_money_event(player, character)
        http_client_mod = MagicMock()
        payment, subsidy, _ = await process_event(
            event, player, character, http_client_mod=http_client_mod
        )

        self.assertEqual(payment, 10_000)
        # Risk premium paid directly, not in returned subsidy
        self.assertEqual(subsidy, 0)
        # Capped at 100%: 10,000 * 1.0 = 10,000 risk premium
        mock_subsidise.assert_called_once_with(
            10_000, character, http_client_mod, message="Risk Premium"
        )

