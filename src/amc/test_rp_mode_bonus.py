from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase, override_settings

from amc.factories import CharacterFactory, PlayerFactory


@patch("amc.pipeline.profit.set_aside_player_savings", new_callable=AsyncMock)
@patch("amc.pipeline.profit.repay_loan_for_profit", new_callable=AsyncMock)
@patch("amc.pipeline.profit.subsidise_player", new_callable=AsyncMock)
@patch("amc.pipeline.profit.transfer_money", new_callable=AsyncMock)
class RPModeBonusTests(TestCase):
    async def test_no_bonus_when_rp_mode_off(
        self, mock_transfer, mock_subsidise, mock_repay_loan, mock_savings
    ):
        mock_repay_loan.return_value = 0

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, rp_mode=False
        )

        session = MagicMock()
        from amc.pipeline.profit import on_player_profit

        await on_player_profit(character, 2000, 10000, session)

        mock_transfer.assert_not_awaited()
        mock_repay_loan.assert_awaited_once()
        args = mock_repay_loan.call_args[0]
        self.assertEqual(args[1], 12000)  # actual_income = 10000 + 2000

    @override_settings(RP_MODE_BONUS_RATE=0.5)
    async def test_rp_mode_bonus_applied(
        self, mock_transfer, mock_subsidise, mock_repay_loan, mock_savings
    ):
        mock_repay_loan.return_value = 0

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, rp_mode=True
        )

        session = MagicMock()
        from amc.pipeline.profit import on_player_profit

        await on_player_profit(character, 2000, 10000, session)

        # actual_income before bonus = 10000 + 2000 = 12000
        # rp_bonus = int(12000 * 0.5) = 6000
        mock_transfer.assert_awaited_once_with(
            session, 6000, "RP Mode Bonus", str(character.player.unique_id)
        )

        # actual_income passed to loan repayment = 12000 + 6000 = 18000
        args = mock_repay_loan.call_args[0]
        self.assertEqual(args[1], 18000)

    @override_settings(RP_MODE_BONUS_RATE=0.5)
    async def test_rp_mode_bonus_with_contract_payment(
        self, mock_transfer, mock_subsidise, mock_repay_loan, mock_savings
    ):
        mock_repay_loan.return_value = 0

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, rp_mode=True
        )

        session = MagicMock()
        from amc.pipeline.profit import on_player_profit

        await on_player_profit(
            character, 2000, 10000, session, contract_payment=5000
        )

        # actual_income before bonus = 10000 + 2000 + 5000 = 17000
        # rp_bonus = int(17000 * 0.5) = 8500
        mock_transfer.assert_awaited_once_with(
            session, 8500, "RP Mode Bonus", str(character.player.unique_id)
        )

        # actual_income passed to loan = 17000 + 8500 = 25500
        args = mock_repay_loan.call_args[0]
        self.assertEqual(args[1], 25500)

    @override_settings(RP_MODE_BONUS_RATE=0.5)
    async def test_rp_mode_bonus_excluded_for_gov_employee(
        self, mock_transfer, mock_subsidise, mock_repay_loan, mock_savings
    ):
        from django.utils import timezone
        from datetime import timedelta

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            rp_mode=True,
            gov_employee_until=timezone.now() + timedelta(hours=12),
            gov_employee_level=1,
            gov_employee_contributions=0,
        )

        session = MagicMock()
        from amc.pipeline.profit import on_player_profit

        with patch("amc.gov_employee.player_donation", new_callable=AsyncMock):
            await on_player_profit(character, 2000, 10000, session)

        # Gov employee path: transfer_money for confiscation, not RP bonus
        transfer_amounts = [call[0][1] for call in mock_transfer.call_args_list]
        self.assertNotIn(6000, transfer_amounts)  # No RP bonus amount

        # Loan/savings not called (gov path returns early)
        mock_repay_loan.assert_not_awaited()
        mock_savings.assert_not_awaited()

    @override_settings(RP_MODE_BONUS_RATE=0.5)
    async def test_rp_mode_bonus_excluded_for_ubi(
        self, mock_transfer, mock_subsidise, mock_repay_loan, mock_savings
    ):
        mock_repay_loan.return_value = 0

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, rp_mode=True
        )

        session = MagicMock()
        from amc.pipeline.profit import on_player_profit

        await on_player_profit(
            character, 0, 6000, session, skip_gov_redirect=True
        )

        # skip_gov_redirect=True (UBI) → no RP bonus
        mock_transfer.assert_not_awaited()

        args = mock_repay_loan.call_args[0]
        self.assertEqual(args[1], 6000)  # No bonus added

    @override_settings(RP_MODE_BONUS_RATE=0.3)
    async def test_custom_bonus_rate(
        self, mock_transfer, mock_subsidise, mock_repay_loan, mock_savings
    ):
        mock_repay_loan.return_value = 0

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, rp_mode=True
        )

        session = MagicMock()
        from amc.pipeline.profit import on_player_profit

        await on_player_profit(character, 0, 10000, session)

        # rp_bonus = int(10000 * 0.3) = 3000
        mock_transfer.assert_awaited_once_with(
            session, 3000, "RP Mode Bonus", str(character.player.unique_id)
        )

        args = mock_repay_loan.call_args[0]
        self.assertEqual(args[1], 13000)  # 10000 + 3000

    @override_settings(RP_MODE_BONUS_RATE=0.5)
    async def test_rp_bonus_zero_when_income_zero(
        self, mock_transfer, mock_subsidise, mock_repay_loan, mock_savings
    ):
        mock_repay_loan.return_value = 0

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, rp_mode=True
        )

        session = MagicMock()
        from amc.pipeline.profit import on_player_profit

        await on_player_profit(character, 0, 0, session)

        # No income → no bonus transfer
        mock_transfer.assert_not_awaited()

    @override_settings(RP_MODE_BONUS_RATE=0.5)
    async def test_rp_bonus_affects_savings(
        self, mock_transfer, mock_subsidise, mock_repay_loan, mock_savings
    ):
        mock_repay_loan.return_value = 5000  # partial loan repayment

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, rp_mode=True
        )

        session = MagicMock()
        from amc.pipeline.profit import on_player_profit

        await on_player_profit(character, 0, 10000, session)

        # actual_income = 10000 + bonus(5000) = 15000
        # savings = 15000 - 5000 (loan) = 10000
        mock_savings.assert_awaited_once()
        savings_amount = mock_savings.call_args[0][1]
        self.assertEqual(savings_amount, 10000)
