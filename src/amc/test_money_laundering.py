import time
import asyncio
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch, AsyncMock
from django.test import TestCase
from django.contrib.gis.geos import Point
from asgiref.sync import sync_to_async
from amc.factories import PlayerFactory, CharacterFactory
from amc.webhook import process_event
from amc.models import (
    DeliveryPoint,
    CharacterLocation,
    CriminalRecord,
)
from django.utils import timezone
from amc_finance.services import get_treasury_fund_balance


def _money_cargo_event(character_guid, player_id, payment=10_000):
    return {
        "hook": "ServerCargoArrived",
        "timestamp": int(time.time()),
        "data": {
            "CharacterGuid": str(character_guid),
            "PlayerId": str(player_id),
            "Cargos": [
                {
                    "Net_CargoKey": "Money",
                    "Net_Payment": payment,
                    "Net_Weight": 50.0,
                    "Net_Damage": 0.0,
                    "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                    "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                }
            ],
        },
    }


async def _setup_character(guid_suffix=""):
    """Create player + character + required related objects."""
    player = await sync_to_async(PlayerFactory)()
    guid = f"money-test-{guid_suffix or player.unique_id}"
    character = await sync_to_async(CharacterFactory)(player=player, guid=guid)
    await CharacterLocation.objects.acreate(
        character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
    )
    await DeliveryPoint.objects.aget_or_create(
        guid="ms1", defaults={"name": "MS1", "coord": Point(0, 0, 0)}
    )
    await DeliveryPoint.objects.aget_or_create(
        guid="md1", defaults={"name": "MD1", "coord": Point(100, 100, 0)}
    )
    return player, character


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
@patch("amc.webhook.announce", new_callable=AsyncMock)
@patch("amc.special_cargo.announce", new_callable=AsyncMock)
class MoneyLaunderingTests(TestCase):
    async def test_money_delivery_creates_criminal_record(
        self, mock_sc_announce, mock_announce, mock_get_treasury, mock_get_rp_mode
    ):
        """Money delivery should create a CriminalRecord for the character."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 1_000_000

        player, character = await _setup_character("cr1")
        event = _money_cargo_event(character.guid, player.unique_id, payment=10_000)

        await process_event(event, player, character)

        record = await CriminalRecord.objects.filter(character=character).afirst()
        self.assertIsNotNone(record)
        self.assertEqual(record.reason, "Money delivery")
        self.assertGreater(record.expires_at, timezone.now())

    async def test_money_delivery_resets_existing_criminal_record(
        self, mock_sc_announce, mock_announce, mock_get_treasury, mock_get_rp_mode
    ):
        """Subsequent Money deliveries should reset the criminal record to 7 days from now."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 1_000_000

        player, character = await _setup_character("cr2")

        # Create existing record
        original_expiry = timezone.now() + timedelta(days=3)
        await CriminalRecord.objects.acreate(
            character=character,
            reason="Money delivery",
            expires_at=original_expiry,
        )

        event = _money_cargo_event(character.guid, player.unique_id, payment=5_000)
        await process_event(event, player, character)

        self.assertEqual(
            await CriminalRecord.objects.filter(character=character).acount(), 1
        )
        record = await CriminalRecord.objects.aget(character=character)
        expected_expiry = timezone.now() + timedelta(days=7)
        self.assertAlmostEqual(
            record.expires_at.timestamp(), expected_expiry.timestamp(), delta=5
        )

    @patch("amc.special_cargo.asyncio.sleep", new_callable=AsyncMock)
    async def test_money_delivery_server_announcement(
        self, mock_sleep, mock_sc_announce, mock_announce, mock_get_treasury, mock_get_rp_mode
    ):
        """Money delivery should trigger a debounced server announcement."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 1_000_000

        player, character = await _setup_character("ann")
        event = _money_cargo_event(character.guid, player.unique_id, payment=15_000)
        http_client = AsyncMock()

        await process_event(event, player, character, http_client=http_client)
        # Let fire-and-forget create_task coroutines complete
        for _ in range(10):
            await asyncio.sleep(0)

        mock_sc_announce.assert_called()
        # Both laundered and secured tasks may fire; find the laundered announcement
        announce_msgs = [call[0][0] for call in mock_sc_announce.call_args_list]
        laundered_msg = next(m for m in announce_msgs if "laundered" in m)
        self.assertIn("15,000", laundered_msg)

    async def test_money_delivery_debounces_announcements(
        self, mock_sc_announce, mock_announce, mock_get_treasury, mock_get_rp_mode
    ):
        """Multiple Money deliveries within the debounce window should accumulate, not spam."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 1_000_000

        player, character = await _setup_character("deb")
        http_client = AsyncMock()

        # Patch create_task to capture but NOT actually schedule the coroutine
        # This prevents the background task from running and calling real announce
        created_tasks = []

        def tracking_create_task(coro):
            created_tasks.append(coro)
            # Close the coroutine to avoid "was never awaited" warning
            coro.close()
            return AsyncMock()

        with patch("amc.special_cargo.asyncio.create_task", side_effect=tracking_create_task):
            # First delivery — should schedule laundering announcement + money_secured
            event1 = _money_cargo_event(character.guid, player.unique_id, payment=10_000)
            await process_event(event1, player, character, http_client=http_client)

            # Second delivery — laundering announcement debounced, only money_secured task
            event2 = _money_cargo_event(character.guid, player.unique_id, payment=20_000)
            await process_event(event2, player, character, http_client=http_client)

        # 3 total tasks: laundered(1st) + secured(1st) + secured(2nd)
        # Laundering announcement is debounced (only 1 task), money_secured fires per-delivery
        laundered_tasks = [t for t in created_tasks if '_announce_laundered' in t.__qualname__]
        self.assertEqual(len(laundered_tasks), 1, "Laundering announcement should be debounced to 1 task")

        # Verify accumulated total in cache (dict format: {total: ..., name: ...})
        from django.core.cache import cache
        cache_key = f"money_laundered:{character.guid}"
        data = await cache.aget(cache_key, {})
        self.assertEqual(data.get("total", 0), 30_000)

    async def test_money_delivery_treasury_cost(
        self, mock_sc_announce, mock_announce, mock_get_treasury, mock_get_rp_mode
    ):
        """Money delivery should deduct 20% of the payment from the treasury."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 1_000_000

        player, character = await _setup_character("tc1")

        initial_balance = Decimal(str(await get_treasury_fund_balance()))

        event = _money_cargo_event(character.guid, player.unique_id, payment=50_000)
        await process_event(event, player, character)

        final_balance = Decimal(str(await get_treasury_fund_balance()))

        expected_cost = Decimal(int(50_000 * 0.20))  # 10,000
        self.assertEqual(initial_balance - final_balance, expected_cost)

    async def test_money_delivery_treasury_cost_multiple_cargos(
        self, mock_sc_announce, mock_announce, mock_get_treasury, mock_get_rp_mode
    ):
        """Multiple Money cargos in one event should sum up for treasury cost."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 1_000_000

        player, character = await _setup_character("tc2")

        initial_balance = Decimal(str(await get_treasury_fund_balance()))

        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "PlayerId": str(player.unique_id),
                "Cargos": [
                    {
                        "Net_CargoKey": "Money",
                        "Net_Payment": 10_000,
                        "Net_Weight": 50.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    },
                    {
                        "Net_CargoKey": "Money",
                        "Net_Payment": 10_000,
                        "Net_Weight": 50.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    },
                    {
                        "Net_CargoKey": "Money",
                        "Net_Payment": 10_000,
                        "Net_Weight": 50.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    },
                ],
            },
        }

        await process_event(event, player, character)

        final_balance = Decimal(str(await get_treasury_fund_balance()))
        expected_cost = Decimal(int(30_000 * 0.20))
        self.assertEqual(initial_balance - final_balance, expected_cost)

    async def test_non_money_delivery_no_treasury_cost(
        self, mock_sc_announce, mock_announce, mock_get_treasury, mock_get_rp_mode
    ):
        """Non-Money cargo deliveries should not incur treasury cost."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 1_000_000

        player, character = await _setup_character("nmc")

        initial_balance = Decimal(str(await get_treasury_fund_balance()))

        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "PlayerId": str(player.unique_id),
                "Cargos": [
                    {
                        "Net_CargoKey": "oranges",
                        "Net_Payment": 50_000,
                        "Net_Weight": 100.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }

        await process_event(event, player, character)

        final_balance = Decimal(str(await get_treasury_fund_balance()))
        self.assertEqual(initial_balance, final_balance)
