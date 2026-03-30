import time
from datetime import timedelta
from unittest.mock import patch, AsyncMock

from asgiref.sync import sync_to_async
from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone

from amc.factories import PlayerFactory, CharacterFactory
from amc.models import (
    CriminalRecord,
    CharacterLocation,
    DeliveryPoint,
)
from amc.webhook import process_event


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
class MoneyCargoHandlerTests(TestCase):
    """Tests for the Money cargo special handler (criminal record, tag, announcement, treasury)."""

    async def _setup_character(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )
        await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="d1", name="D1", coord=Point(100, 100, 0)
        )
        return player, character

    def _money_event(self, character, payment=5000):
        return {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {
                        "Net_CargoKey": "Money",
                        "Net_Payment": payment,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_criminal_record_created_on_first_money_delivery(
        self, mock_treasury_expense, mock_refresh, mock_get_treasury, mock_get_rp_mode,
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._money_event(character)
        await process_event(event, player, character)

        record = await CriminalRecord.objects.filter(character=character).afirst()
        self.assertIsNotNone(record)
        self.assertEqual(record.reason, "Money delivery")
        # Expires ~7 days from now
        self.assertAlmostEqual(
            record.expires_at.timestamp(),
            (timezone.now() + timedelta(days=7)).timestamp(),
            delta=5,
        )

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_criminal_record_reset_on_repeat_delivery(
        self, mock_treasury_expense, mock_refresh, mock_get_treasury, mock_get_rp_mode,
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        # Pre-existing record expiring in 3 days
        original_expiry = timezone.now() + timedelta(days=3)
        await CriminalRecord.objects.acreate(
            character=character, reason="Money delivery", expires_at=original_expiry,
        )

        event = self._money_event(character)
        await process_event(event, player, character)

        records = [r async for r in CriminalRecord.objects.filter(character=character)]
        self.assertEqual(len(records), 1, "Should not create a second record")
        # Should be reset to 7 days from now (not extended from original expiry)
        self.assertAlmostEqual(
            records[0].expires_at.timestamp(),
            (timezone.now() + timedelta(days=7)).timestamp(),
            delta=5,
        )

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_player_tag_refreshed(
        self, mock_treasury_expense, mock_refresh, mock_get_treasury, mock_get_rp_mode,
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._money_event(character)
        await process_event(event, player, character)

        mock_refresh.assert_called_once_with(character, None)

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_treasury_expense_recorded(
        self, mock_treasury_expense, mock_refresh, mock_get_treasury, mock_get_rp_mode,
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._money_event(character, payment=10_000)
        await process_event(event, player, character)

        # 20% of 10,000 = 2,000
        mock_treasury_expense.assert_called_once_with(2_000, "Money Laundering Cost")

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_non_money_cargo_no_special_handler(
        self, mock_treasury_expense, mock_refresh, mock_get_treasury, mock_get_rp_mode,
    ):
        """Non-Money cargos should not trigger criminal record or treasury expense."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {
                        "Net_CargoKey": "oranges",
                        "Net_Payment": 10_000,
                        "Net_Weight": 100.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }
        await process_event(event, player, character)

        self.assertEqual(await CriminalRecord.objects.filter(character=character).acount(), 0)
        mock_refresh.assert_not_called()
        mock_treasury_expense.assert_not_called()

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_criminal_laundered_total_accumulated(
        self, mock_treasury_expense, mock_refresh, mock_get_treasury, mock_get_rp_mode,
    ):
        """criminal_laundered_total is incremented by money payment amount."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._money_event(character, payment=50_000)
        await process_event(event, player, character)

        await character.arefresh_from_db()
        self.assertEqual(character.criminal_laundered_total, 50_000)

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_criminal_laundered_total_accumulates_across_deliveries(
        self, mock_treasury_expense, mock_refresh, mock_get_treasury, mock_get_rp_mode,
    ):
        """Multiple deliveries accumulate into criminal_laundered_total."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event1 = self._money_event(character, payment=60_000)
        await process_event(event1, player, character)

        event2 = self._money_event(character, payment=50_000)
        await process_event(event2, player, character)

        await character.arefresh_from_db()
        self.assertEqual(character.criminal_laundered_total, 110_000)

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_criminal_level_increases_with_total(
        self, mock_treasury_expense, mock_refresh, mock_get_treasury, mock_get_rp_mode,
    ):
        """Criminal level increases after crossing 50k threshold."""
        from amc.special_cargo import calculate_criminal_level

        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        # First delivery: 50k → level 2
        event1 = self._money_event(character, payment=50_000)
        await process_event(event1, player, character)
        await character.arefresh_from_db()
        self.assertEqual(calculate_criminal_level(character.criminal_laundered_total), 2)

        # Second delivery: 60k → total 110k → level 3
        event2 = self._money_event(character, payment=60_000)
        await process_event(event2, player, character)
        await character.arefresh_from_db()
        self.assertEqual(calculate_criminal_level(character.criminal_laundered_total), 3)
