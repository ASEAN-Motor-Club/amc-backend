from datetime import timedelta
from unittest.mock import patch, AsyncMock, MagicMock
from django.test import TestCase
from django.utils import timezone
from asgiref.sync import sync_to_async
from amc.factories import PlayerFactory, CharacterFactory
from amc.gov_employee import (
    calculate_gov_level,
    make_gov_name,
    strip_gov_name,
    activate_gov_role,
    deactivate_gov_role,
    redirect_income_to_treasury,
    expire_gov_employees,
    GOV_LEVEL_STEP,
)
from amc.command_framework import CommandContext
from amc.commands.finance import cmd_workforgov


class GovLevelCalculationTests(TestCase):
    def test_level_1_at_zero(self):
        self.assertEqual(calculate_gov_level(0), 1)

    def test_level_1_below_threshold(self):
        self.assertEqual(calculate_gov_level(499_999), 1)

    def test_level_2_at_threshold(self):
        self.assertEqual(calculate_gov_level(500_000), 2)

    def test_level_scales_infinitely(self):
        self.assertEqual(calculate_gov_level(4_500_000), 10)
        self.assertEqual(calculate_gov_level(49_500_000), 100)

    def test_step_size(self):
        self.assertEqual(GOV_LEVEL_STEP, 500_000)


class GovNameTests(TestCase):
    def test_make_gov_name(self):
        self.assertEqual(make_gov_name("PlayerOne", 1), "[GOV1] PlayerOne")

    def test_make_gov_name_strips_existing_tag(self):
        self.assertEqual(make_gov_name("[GOV1] PlayerOne", 3), "[GOV3] PlayerOne")

    def test_strip_gov_name(self):
        self.assertEqual(strip_gov_name("[GOV1] PlayerOne"), "PlayerOne")

    def test_strip_gov_name_no_tag(self):
        self.assertEqual(strip_gov_name("PlayerOne"), "PlayerOne")

    def test_strip_gov_name_case_insensitive(self):
        self.assertEqual(strip_gov_name("[gov2] Test"), "Test")


class ActivateDeactivateTests(TestCase):
    @patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
    async def test_activate_gov_role(self, mock_set_name):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="test-guid-123"
        )

        session = MagicMock()
        await activate_gov_role(character, session)

        await character.arefresh_from_db()
        self.assertIsNotNone(character.gov_employee_until)
        self.assertTrue(character.is_gov_employee)
        self.assertEqual(character.gov_employee_level, 1)
        self.assertIn("[G1]", character.custom_name)
        mock_set_name.assert_awaited_once()

    @patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
    async def test_activate_with_existing_contributions(self, mock_set_name):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="test-guid-456", gov_employee_contributions=1_500_000
        )

        await activate_gov_role(character, MagicMock())

        await character.arefresh_from_db()
        self.assertEqual(character.gov_employee_level, 4)
        self.assertIn("[G4]", character.custom_name)

    @patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
    async def test_deactivate_gov_role(self, mock_set_name):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            guid="test-guid-789",
            gov_employee_until=timezone.now() + timedelta(hours=12),
            gov_employee_level=2,
            custom_name="[GOV2] TestName",
        )

        session = MagicMock()
        await deactivate_gov_role(character, session)

        await character.arefresh_from_db()
        self.assertIsNone(character.gov_employee_until)
        self.assertFalse(character.is_gov_employee)
        # custom_name should be cleared if it matches original name
        mock_set_name.assert_awaited_once()

    @patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
    async def test_deactivate_restores_original_name(self, mock_set_name):
        player = await sync_to_async(PlayerFactory)()
        original_name = "OriginalPlayer"
        character = await sync_to_async(CharacterFactory)(
            player=player,
            name=original_name,
            guid="test-guid-aaa",
            gov_employee_until=timezone.now() + timedelta(hours=12),
            gov_employee_level=1,
            custom_name="[GOV1] OriginalPlayer",
        )

        await deactivate_gov_role(character, MagicMock())

        await character.arefresh_from_db()
        # Name should be restored
        call_args = mock_set_name.call_args
        restored_name = call_args[0][2]  # 3rd positional arg
        self.assertEqual(restored_name, original_name)


class IncomeRedirectionTests(TestCase):
    @patch("amc.gov_employee.player_donation", new_callable=AsyncMock)
    @patch("amc.gov_employee.announce", new_callable=AsyncMock, create=True)
    async def test_redirect_income_to_treasury(self, mock_announce, mock_donation):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            gov_employee_contributions=0,
            gov_employee_level=1,
        )

        await redirect_income_to_treasury(100_000, character, "Test Redirect")

        mock_donation.assert_awaited_once_with(
            100_000, character, description="Test Redirect"
        )
        await character.arefresh_from_db()
        self.assertEqual(character.gov_employee_contributions, 100_000)

    @patch("amc.gov_employee.player_donation", new_callable=AsyncMock)
    async def test_redirect_updates_level(self, mock_donation):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            gov_employee_contributions=400_000,
            gov_employee_level=1,
        )

        await redirect_income_to_treasury(200_000, character, "Test Redirect")

        await character.arefresh_from_db()
        self.assertEqual(character.gov_employee_contributions, 600_000)
        self.assertEqual(character.gov_employee_level, 2)


class WebhookPipelineTests(TestCase):
    @patch("amc.webhook.subsidise_player", new_callable=AsyncMock)
    @patch("amc.webhook.transfer_money", new_callable=AsyncMock)
    @patch("amc.gov_employee.player_donation", new_callable=AsyncMock)
    async def test_on_player_profit_gov_employee(self, mock_donation, mock_transfer, mock_subsidy):
        from amc.webhook import on_player_profit

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            gov_employee_until=timezone.now() + timedelta(hours=12),
            gov_employee_level=1,
            gov_employee_contributions=0,
        )

        session = MagicMock()
        # subsidy=5000 (cargo subsidy, paid separately by system)
        # base_payment=10000 (what the game deposited into wallet)
        await on_player_profit(character, 5000, 10000, session)

        # transfer_money calls:
        # 1. Confiscate base_payment (10000) from wallet
        # 2. Confiscate subsidy (5000) back after subsidise_player deposits it
        self.assertEqual(mock_transfer.await_count, 2)
        # First call: wallet confiscation
        self.assertEqual(mock_transfer.call_args_list[0][0][1], -10000)
        # Second call: subsidy confiscation
        self.assertEqual(mock_transfer.call_args_list[1][0][1], -5000)

        # subsidise_player should be called with the subsidy amount
        mock_subsidy.assert_awaited_once()

        # Ledger: player_donation called twice:
        # 1. redirect_income_to_treasury(10000) for base_payment confiscation
        # 2. redirect_income_to_treasury(0, contribution=5000) for subsidy
        self.assertEqual(mock_donation.await_count, 2)
        self.assertEqual(mock_donation.call_args_list[0][0][0], 10000)
        self.assertEqual(mock_donation.call_args_list[1][0][0], 0)

        # Contribution should track base_payment + subsidy (15000)
        await character.arefresh_from_db()
        self.assertEqual(character.gov_employee_contributions, 15000)

    @patch("amc.webhook.subsidise_player", new_callable=AsyncMock)
    @patch("amc.webhook.transfer_money", new_callable=AsyncMock)
    @patch("amc.gov_employee.player_donation", new_callable=AsyncMock)
    async def test_on_player_profit_gov_contract_burned(
        self, mock_donation, mock_transfer, mock_subsidy
    ):
        """Contract payment is burned: confiscated from wallet but not deposited to treasury."""
        from amc.webhook import on_player_profit

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            gov_employee_until=timezone.now() + timedelta(hours=12),
            gov_employee_level=1,
            gov_employee_contributions=0,
        )

        session = MagicMock()
        # subsidy=5000, base_payment=10000 (game wallet deposit), contract=3000
        await on_player_profit(
            character, 5000, 10000, session, contract_payment=3000
        )

        # transfer_money calls:
        # 1. Wallet confiscation = base_payment + contract = 10000 + 3000 = 13000
        # 2. Subsidy confiscation = 5000
        self.assertEqual(mock_transfer.await_count, 2)
        self.assertEqual(mock_transfer.call_args_list[0][0][1], -13000)
        self.assertEqual(mock_transfer.call_args_list[1][0][1], -5000)

        # Ledger: player_donation called twice:
        # 1. redirect_income_to_treasury(10000) for base_payment confiscation
        # 2. redirect_income_to_treasury(0, contribution=5000) for subsidy
        self.assertEqual(mock_donation.await_count, 2)
        self.assertEqual(mock_donation.call_args_list[0][0][0], 10000)
        self.assertEqual(mock_donation.call_args_list[1][0][0], 0)

        # Contribution tracks base_payment + subsidy (15000), excludes contract
        await character.arefresh_from_db()
        self.assertEqual(character.gov_employee_contributions, 15000)

    @patch("amc.webhook.subsidise_player", new_callable=AsyncMock)
    @patch("amc.webhook.repay_loan_for_profit", new_callable=AsyncMock)
    @patch("amc.webhook.set_aside_player_savings", new_callable=AsyncMock)
    async def test_on_player_profit_normal(self, mock_savings, mock_loan, mock_subsidy):
        from amc.webhook import on_player_profit

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)

        mock_loan.return_value = 0
        session = MagicMock()
        await on_player_profit(character, 5000, 15000, session)

        # Normal flow: subsidy, loan, savings
        mock_subsidy.assert_awaited_once()
        mock_loan.assert_awaited_once()


class JobBonusRedirectionTests(TestCase):
    @patch("amc.gov_employee.player_donation", new_callable=AsyncMock)
    @patch("amc.jobs.send_fund_to_player", new_callable=AsyncMock)
    @patch("amc.jobs.announce", new_callable=AsyncMock)
    async def test_job_bonus_redirected_for_gov(
        self, mock_announce, mock_send_fund, mock_donation
    ):
        from amc.jobs import on_delivery_job_fulfilled
        from amc.models import DeliveryJob, Delivery

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            gov_employee_until=timezone.now() + timedelta(hours=12),
            gov_employee_level=1,
            gov_employee_contributions=0,
        )

        job = await sync_to_async(
            lambda: DeliveryJob.objects.create(
                name="Test Job",
                cargo_key="test_cargo",
                quantity_requested=10,
                quantity_fulfilled=10,
                bonus_multiplier=1.0,
                completion_bonus=50000,
                expired_at=timezone.now() + timedelta(hours=2),
            )
        )()

        await Delivery.objects.acreate(
            timestamp=timezone.now(),
            character=character,
            job=job,
            cargo_key="test_cargo",
            quantity=10,
            payment=50000,
        )

        session = MagicMock()
        await on_delivery_job_fulfilled(job, session)

        # Gov employee should NOT get fund, but redirect should happen
        mock_send_fund.assert_not_awaited()
        mock_donation.assert_awaited()


class ExpireGovEmployeesTests(TestCase):
    @patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
    async def test_expire_deactivates_expired_roles(self, mock_set_name):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            guid="expire-guid",
            gov_employee_until=timezone.now() - timedelta(hours=1),
            gov_employee_level=2,
            custom_name="[GOV2] ExpiredPlayer",
        )

        ctx = {"http_client_mod": MagicMock()}
        await expire_gov_employees(ctx)

        await character.arefresh_from_db()
        self.assertIsNone(character.gov_employee_until)
        self.assertFalse(character.is_gov_employee)

    async def test_expire_does_not_touch_active_roles(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            gov_employee_until=timezone.now() + timedelta(hours=12),
            gov_employee_level=1,
        )

        ctx = {"http_client_mod": MagicMock()}
        await expire_gov_employees(ctx)

        await character.arefresh_from_db()
        self.assertTrue(character.is_gov_employee)


class RenameGovTagProtectionTests(TestCase):
    async def test_rename_blocks_gov_tag_for_non_gov(self):
        from amc.commands.general import cmd_rename

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="rename-guid"
        )

        ctx = MagicMock(spec=CommandContext)
        ctx.reply = AsyncMock()
        ctx.character = character
        ctx.http_client_mod = MagicMock()

        await cmd_rename(ctx, "[GOV1] Cheater")

        ctx.reply.assert_called_once()
        self.assertIn("reserved", ctx.reply.call_args[0][0])

    @patch("amc.commands.general.set_character_name", new_callable=AsyncMock)
    async def test_rename_allows_gov_tag_for_gov_employee(self, mock_set_name):
        from amc.commands.general import cmd_rename

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            guid="rename-gov-guid",
            gov_employee_until=timezone.now() + timedelta(hours=12),
            gov_employee_level=1,
        )

        ctx = MagicMock(spec=CommandContext)
        ctx.reply = AsyncMock()
        ctx.character = character
        ctx.http_client_mod = MagicMock()

        await cmd_rename(ctx, "[GOV1] GovPlayer")

        # Should not be blocked
        mock_set_name.assert_awaited_once()


class WorkforgovCommandTests(TestCase):
    @patch("amc.gov_employee.activate_gov_role", new_callable=AsyncMock)
    @patch("amc.commands.finance.with_verification_code")
    async def test_workforgov_activates_with_correct_code(
        self, mock_verify, mock_activate
    ):
        mock_verify.return_value = ("ABC123", True)

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="cmd-guid"
        )

        ctx = MagicMock(spec=CommandContext)
        ctx.reply = AsyncMock()
        ctx.announce = AsyncMock()
        ctx.character = character
        ctx.http_client_mod = MagicMock()

        await cmd_workforgov(ctx, "ABC123")

        mock_activate.assert_awaited_once()

    @patch("amc.mod_server.set_character_name", new_callable=AsyncMock)
    async def test_workforgov_shows_status_when_active(self, mock_set_name):
        # Create several characters with varying contributions
        player1 = await sync_to_async(PlayerFactory)()
        await sync_to_async(CharacterFactory)(
            player=player1,
            name="TopPlayer",
            gov_employee_contributions=5_000_000,
        )

        player2 = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player2,
            guid="cmd-active-guid",
            name="TestPlayer",
            gov_employee_until=timezone.now() + timedelta(hours=12),
            gov_employee_level=3,
            gov_employee_contributions=1_200_000,
        )

        player3 = await sync_to_async(PlayerFactory)()
        await sync_to_async(CharacterFactory)(
            player=player3,
            name="LowPlayer",
            gov_employee_contributions=100_000,
        )

        # Character with zero contributions should be excluded
        player4 = await sync_to_async(PlayerFactory)()
        await sync_to_async(CharacterFactory)(
            player=player4,
            name="ZeroPlayer",
            gov_employee_contributions=0,
        )

        ctx = MagicMock(spec=CommandContext)
        ctx.reply = AsyncMock()
        ctx.character = character
        ctx.http_client_mod = MagicMock()

        await cmd_workforgov(ctx)

        ctx.reply.assert_called_once()
        output = ctx.reply.call_args[0][0]
        self.assertIn("GOV3", output)
        self.assertIn("Status", output)
        # Ranking: TopPlayer > TestPlayer > LowPlayer = 3 total, rank #2
        self.assertIn("#2 out of 3", output)
        # Leaderboard should include all 3 with positive contributions
        self.assertIn("TopPlayer", output)
        self.assertIn("TestPlayer", output)
        self.assertIn("LowPlayer", output)
        # ZeroPlayer should be excluded
        self.assertNotIn("ZeroPlayer", output)


class IsGovEmployeePropertyTests(TestCase):
    async def test_active_gov_employee(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            gov_employee_until=timezone.now() + timedelta(hours=12),
        )
        self.assertTrue(character.is_gov_employee)

    async def test_expired_gov_employee(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            gov_employee_until=timezone.now() - timedelta(hours=1),
        )
        self.assertFalse(character.is_gov_employee)

    async def test_null_gov_employee(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            gov_employee_until=None,
        )
        self.assertFalse(character.is_gov_employee)


class DailyGovEmployeeSummaryTaskTests(TestCase):
    async def test_build_daily_gov_employee_embed(self):
        from amc_cogs.economy import EconomyCog
        from amc.gov_employee import player_donation

        # Create two gov employees and one normal character
        player1 = await sync_to_async(PlayerFactory)()
        char1 = await sync_to_async(CharacterFactory)(
            player=player1,
            name="Alice",
            gov_employee_level=3,
        )

        player2 = await sync_to_async(PlayerFactory)()
        char2 = await sync_to_async(CharacterFactory)(
            player=player2,
            name="Bob",
            gov_employee_level=1,
        )

        player3 = await sync_to_async(PlayerFactory)()
        char_normal = await sync_to_async(CharacterFactory)(
            player=player3,
            name="Charlie",
        )

        # Alice earns 15k and gets a 5k job bonus
        await player_donation(15000, char1, description="Government Service - Earnings")
        await player_donation(5000, char1, description="Government Service - Job Bonus")

        # Bob earns 8k
        await player_donation(8000, char2, description="Government Service - Earnings")

        # Charlie donates 100k normally to treasury (should NOT be in the gov summary)
        await player_donation(100000, char_normal, description="Player Donation")

        # Build the embed
        bot_mock = MagicMock()
        cog = EconomyCog(bot=bot_mock)
        embed = await cog.build_daily_gov_employee_embed()

        # Assertions
        self.assertEqual(embed.title, "🏛️ Daily Government Employee Report")

        # Total amount treasury raised (15k + 5k + 8k = 28k) -> Charlie's 100k shouldn't be here
        first_field = embed.fields[0]
        self.assertIn("28,000", first_field.name)
        self.assertIn("From **2** active civil servant", first_field.value)

        # Top Contributors breakdown
        second_field = embed.fields[1]
        self.assertEqual(second_field.name, "Top Contributors")
        self.assertIn("**[GOV3] Alice:** `20,000.00`", second_field.value)
        self.assertIn("**[GOV1] Bob:** `8,000.00`", second_field.value)
        self.assertNotIn("Charlie", second_field.value)
