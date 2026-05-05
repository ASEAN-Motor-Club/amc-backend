from decimal import Decimal
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from django.test import TestCase
from django.utils import timezone

from amc.models import Character, Player
from amc.ubi import handout_ubi, ACTIVE_GRANT_AMOUNT, MAX_LEVEL


def _make_ctx(http_client=None, http_client_mod=None):
    ctx = {}
    ctx["http_client"] = http_client or AsyncMock()
    ctx["http_client_mod"] = http_client_mod or AsyncMock()
    return ctx


class HandoutUbiLoanRepaymentTest(TestCase):
    """Test that UBI goes through on_player_profit pipeline."""

    def setUp(self):
        self.player = Player.objects.create(unique_id=9999)
        self.character = Character.objects.create(
            player=self.player,
            name="TestDriver",
            guid="aabbccdd11223344aabbccdd11223344",
            driver_level=100,
        )

    def _mock_players(self):
        return [
            (
                str(self.player.unique_id),
                {"character_guid": self.character.guid},
            )
        ]

    def _expected_amount(self):
        grant = ACTIVE_GRANT_AMOUNT
        amount = min(
            Decimal(str(grant)),
            self.character.driver_level
            * Decimal(str(grant))
            * Decimal(str(self.character.ubi_multiplier))
            / MAX_LEVEL,
        )
        return amount

    @patch("amc.ubi.calculate_treasury_multiplier", return_value=1.0)
    @patch("amc.ubi.on_player_profit", new_callable=AsyncMock)
    @patch("amc.ubi.transfer_money", new_callable=AsyncMock)
    @patch("amc.ubi.send_fund_to_player_wallet", new_callable=AsyncMock)
    @patch("amc.ubi.get_players", new_callable=AsyncMock)
    @patch("amc.ubi.get_treasury_fund_balance", new_callable=AsyncMock)
    async def test_no_loan_no_repayment(
        self,
        mock_treasury_balance,
        mock_get_players,
        mock_send,
        mock_transfer,
        mock_on_player_profit,
        mock_treasury_mult,
    ):
        """When no loan exists, UBI is paid and on_player_profit is called."""
        mock_treasury_balance.return_value = Decimal(50_000_000)
        mock_get_players.return_value = self._mock_players()

        ctx = _make_ctx(http_client_mod=mock_transfer)
        with patch(
            "amc.ubi.CharacterLocation.batch_get_character_activity",
            new_callable=AsyncMock,
            return_value={self.character.id: (True, True)},
        ):
            await handout_ubi(ctx)

        mock_send.assert_called_once()
        mock_transfer.assert_called_once()
        assert mock_transfer.call_args[0][1] > 0
        mock_on_player_profit.assert_called_once()

    @patch("amc.ubi.calculate_treasury_multiplier", return_value=1.0)
    @patch("amc.ubi.on_player_profit", new_callable=AsyncMock)
    @patch("amc.ubi.transfer_money", new_callable=AsyncMock)
    @patch("amc.ubi.send_fund_to_player_wallet", new_callable=AsyncMock)
    @patch("amc.ubi.get_players", new_callable=AsyncMock)
    @patch("amc.ubi.get_treasury_fund_balance", new_callable=AsyncMock)
    async def test_loan_larger_than_ubi(
        self,
        mock_treasury_balance,
        mock_get_players,
        mock_send,
        mock_transfer,
        mock_on_player_profit,
        mock_treasury_mult,
    ):
        """When loan > UBI, on_player_profit handles repayment via pipeline."""
        mock_treasury_balance.return_value = Decimal(50_000_000)
        mock_get_players.return_value = self._mock_players()

        ctx = _make_ctx(http_client_mod=mock_transfer)
        with patch(
            "amc.ubi.CharacterLocation.batch_get_character_activity",
            new_callable=AsyncMock,
            return_value={self.character.id: (True, True)},
        ):
            await handout_ubi(ctx)

        expected = int(self._expected_amount())

        mock_on_player_profit.assert_called_once()
        call_args = mock_on_player_profit.call_args
        self.assertEqual(call_args[1]["subsidy"], 0)
        self.assertEqual(call_args[1]["base_payment"], expected)

    @patch("amc.ubi.calculate_treasury_multiplier", return_value=1.0)
    @patch("amc.ubi.on_player_profit", new_callable=AsyncMock)
    @patch("amc.ubi.transfer_money", new_callable=AsyncMock)
    @patch("amc.ubi.send_fund_to_player_wallet", new_callable=AsyncMock)
    @patch("amc.ubi.get_players", new_callable=AsyncMock)
    @patch("amc.ubi.get_treasury_fund_balance", new_callable=AsyncMock)
    async def test_loan_smaller_than_ubi(
        self,
        mock_treasury_balance,
        mock_get_players,
        mock_send,
        mock_transfer,
        mock_on_player_profit,
        mock_treasury_mult,
    ):
        """When loan < UBI, on_player_profit handles partial repayment via pipeline."""
        mock_treasury_balance.return_value = Decimal(50_000_000)
        mock_get_players.return_value = self._mock_players()

        ctx = _make_ctx(http_client_mod=mock_transfer)
        with patch(
            "amc.ubi.CharacterLocation.batch_get_character_activity",
            new_callable=AsyncMock,
            return_value={self.character.id: (True, True)},
        ):
            await handout_ubi(ctx)

        mock_on_player_profit.assert_called_once()

    @patch("amc.ubi.calculate_treasury_multiplier", return_value=1.0)
    @patch("amc.ubi.on_player_profit", new_callable=AsyncMock)
    @patch("amc.ubi.transfer_money", new_callable=AsyncMock)
    @patch("amc.ubi.send_fund_to_player_wallet", new_callable=AsyncMock)
    @patch("amc.ubi.get_players", new_callable=AsyncMock)
    @patch("amc.ubi.get_treasury_fund_balance", new_callable=AsyncMock)
    async def test_gov_employee_with_loan(
        self,
        mock_treasury_balance,
        mock_get_players,
        mock_send,
        mock_transfer,
        mock_on_player_profit,
        mock_treasury_mult,
    ):
        """Gov employee gets 2x UBI with skip_gov_redirect=True so salary is kept."""
        mock_treasury_balance.return_value = Decimal(50_000_000)
        self.character.gov_employee_until = timezone.now() + timedelta(hours=12)
        await self.character.asave()

        mock_get_players.return_value = self._mock_players()

        ctx = _make_ctx(http_client_mod=mock_transfer)
        with patch(
            "amc.ubi.CharacterLocation.batch_get_character_activity",
            new_callable=AsyncMock,
            return_value={self.character.id: (True, True)},
        ):
            await handout_ubi(ctx)

        expected_base = int(self._expected_amount())
        expected_gov = expected_base * 2

        mock_on_player_profit.assert_called_once()
        call_args = mock_on_player_profit.call_args
        self.assertEqual(call_args[1]["base_payment"], expected_gov)
        self.assertTrue(call_args[1]["skip_gov_redirect"])

    @patch("amc.ubi.on_player_profit", new_callable=AsyncMock)
    @patch("amc.ubi.transfer_money", new_callable=AsyncMock)
    @patch("amc.ubi.send_fund_to_player_wallet", new_callable=AsyncMock)
    @patch("amc.ubi.get_players", new_callable=AsyncMock)
    @patch("amc.ubi.get_treasury_fund_balance", new_callable=AsyncMock)
    async def test_ubi_stops_below_floor(
        self,
        mock_treasury_balance,
        mock_get_players,
        mock_send,
        mock_transfer,
        mock_on_player_profit,
    ):
        """When treasury is at or below floor, no UBI is paid."""
        mock_treasury_balance.return_value = Decimal(5_000_000)
        mock_get_players.return_value = self._mock_players()

        ctx = _make_ctx(http_client_mod=mock_transfer)
        with patch(
            "amc.ubi.CharacterLocation.batch_get_character_activity",
            new_callable=AsyncMock,
            return_value={self.character.id: (True, True)},
        ):
            await handout_ubi(ctx)

        mock_send.assert_not_called()
        mock_transfer.assert_not_called()
        mock_on_player_profit.assert_not_called()

    @patch("amc.ubi.calculate_treasury_multiplier", return_value=0.5)
    @patch("amc.ubi.on_player_profit", new_callable=AsyncMock)
    @patch("amc.ubi.transfer_money", new_callable=AsyncMock)
    @patch("amc.ubi.send_fund_to_player_wallet", new_callable=AsyncMock)
    @patch("amc.ubi.get_players", new_callable=AsyncMock)
    @patch("amc.ubi.get_treasury_fund_balance", new_callable=AsyncMock)
    async def test_ubi_scales_with_treasury(
        self,
        mock_treasury_balance,
        mock_get_players,
        mock_send,
        mock_transfer,
        mock_on_player_profit,
        mock_treasury_mult,
    ):
        """UBI scales via calculate_treasury_multiplier."""
        mock_treasury_balance.return_value = Decimal(50_000_000)
        mock_get_players.return_value = self._mock_players()

        ctx = _make_ctx(http_client_mod=mock_transfer)
        with patch(
            "amc.ubi.CharacterLocation.batch_get_character_activity",
            new_callable=AsyncMock,
            return_value={self.character.id: (True, True)},
        ):
            await handout_ubi(ctx)

        mock_treasury_mult.assert_called_once()
        mock_send.assert_called_once()
        sent_amount = mock_send.call_args[0][0]
        expected_full = self._expected_amount()
        expected_scaled = expected_full * Decimal("0.5")
        self.assertAlmostEqual(float(sent_amount), float(expected_scaled), places=2)
