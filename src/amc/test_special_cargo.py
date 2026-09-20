import time
from unittest.mock import patch, AsyncMock

from asgiref.sync import sync_to_async
from django.contrib.gis.geos import Point
from django.test import TestCase

from amc.factories import PlayerFactory, CharacterFactory
from amc.models import (
    CharacterLocation,
    DeliveryPoint,
)
from amc.webhook import process_event


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
class MoneyCargoHandlerTests(TestCase):
    """Tests for the Money cargo special handler (criminal score, treasury)."""

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

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_criminal_score_accumulates_on_first_money_delivery(
        self,
        mock_treasury_expense,
        mock_get_treasury,
        mock_get_rp_mode,
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._money_event(character)
        await process_event(event, player, character)

        await character.arefresh_from_db(fields=["criminal_score", "last_illicit_delivery_at"])
        self.assertEqual(character.criminal_score, 5_000)
        self.assertIsNotNone(character.last_illicit_delivery_at)

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_criminal_score_accumulates_on_repeat_delivery(
        self,
        mock_treasury_expense,
        mock_get_treasury,
        mock_get_rp_mode,
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        # Pre-existing score
        character.criminal_score = 30_000
        await character.asave(update_fields=["criminal_score"])

        event = self._money_event(character, payment=5_000)
        await process_event(event, player, character)

        await character.arefresh_from_db(fields=["criminal_score"])
        self.assertEqual(character.criminal_score, 35_000)

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_treasury_expense_recorded(
        self,
        mock_treasury_expense,
        mock_get_treasury,
        mock_get_rp_mode,
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._money_event(character, payment=10_000)
        await process_event(event, player, character)

        # 20% of 10,000 = 2,000
        mock_treasury_expense.assert_called_once_with(2_000, "Money Laundering Cost")

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_non_money_cargo_no_special_handler(
        self,
        mock_treasury_expense,
        mock_get_treasury,
        mock_get_rp_mode,
    ):
        """Non-Money cargos should not accumulate criminal score or treasury expense."""
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

        await character.arefresh_from_db(fields=["criminal_score"])
        self.assertEqual(character.criminal_score, 0)
        mock_treasury_expense.assert_not_called()

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_criminal_score_accumulated(
        self,
        mock_treasury_expense,
        mock_get_treasury,
        mock_get_rp_mode,
    ):
        """criminal_score is incremented by the money payment amount."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._money_event(character, payment=50_000)
        await process_event(event, player, character)

        await character.arefresh_from_db(fields=["criminal_score"])
        self.assertEqual(character.criminal_score, 50_000)

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_criminal_score_accumulates_across_deliveries(
        self,
        mock_treasury_expense,
        mock_get_treasury,
        mock_get_rp_mode,
    ):
        """Multiple deliveries accumulate into criminal_score."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event1 = self._money_event(character, payment=60_000)
        await process_event(event1, player, character)

        event2 = self._money_event(character, payment=50_000)
        await process_event(event2, player, character)

        await character.arefresh_from_db(fields=["criminal_score"])
        self.assertEqual(character.criminal_score, 110_000)

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_criminal_level_increases_with_total(
        self,
        mock_treasury_expense,
        mock_get_treasury,
        mock_get_rp_mode,
    ):
        """Criminal level increases after crossing 50k threshold."""
        from amc.special_cargo import calculate_criminal_level

        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        # First delivery: 50k → level 2
        event1 = self._money_event(character, payment=50_000)
        await process_event(event1, player, character)
        await character.arefresh_from_db(fields=["criminal_score"])
        self.assertEqual(
            calculate_criminal_level(character.criminal_score), 2
        )

        # Second delivery: 60k → total 110k → level 3
        event2 = self._money_event(character, payment=60_000)
        await process_event(event2, player, character)
        await character.arefresh_from_db(fields=["criminal_score"])
        self.assertEqual(
            calculate_criminal_level(character.criminal_score), 3
        )
