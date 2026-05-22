"""Tests for amc_cogs.housing — house rent extension and rebate logic.

Regression tests for the bug where players RECEIVED money when extending
their house rent. The rebate (based on delivery earnings and optional license)
was intended to reduce the rental cost. Instead, the confirm handler charged
the discounted amount AND THEN sent the full rebate back as cash — a double
benefit that could even result in net profit for the player.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase, override_settings
from django.utils import timezone

from amc.factories import CharacterFactory, PlayerFactory
from amc.models import Delivery, HousingLicense, ServerStatus
from amc_cogs.housing import CharacterSelect, ExtendConfirmView
from amc_cogs.housing_market import get_market_multiplier


DEFAULT_MARKET_BREAKDOWN = {
    "avg_players": 10.0,
    "player_factor": 1.0,
    "avg_rent_pct": 100.0,
    "rent_health": 1.0,
    "multiplier": 1.0,
}


def _mock_market_multiplier(houses, max_rent_days, **kwargs):
    from amc_cogs.housing_market import cache

    cached = cache.get("housing_market_multiplier")
    if cached is not None:
        return cached["multiplier"], cached
    return 1.0, DEFAULT_MARKET_BREAKDOWN


class ExtendConfirmViewConfirmTests(TestCase):
    """Tests for ExtendConfirmView.confirm — the rebate money flow.

    The confirm handler should:
      1. Charge the player net_cost (rent_cost − rebate) from their bank
      2. Call extend_house_rent to extend the rent on the game server
      3. Record net_cost as treasury income

    It should NOT:
      - Send any money back to the player (no transfer_money / send_fund_to_player_wallet)
    """

    def _make_view(
        self,
        character,
        player,
        *,
        rent_cost=1000,
        rebate=0,
        net_cost=1000,
    ):
        return ExtendConfirmView(
            character=character,
            player=player,
            house_guid="test-house-guid",
            house_key="TestHouse_01",
            extend_seconds=86400,
            extend_days=1,
            rent_cost=rent_cost,
            rebate=rebate,
            net_cost=net_cost,
            mod_session=AsyncMock(),
        )

    @patch("amc_cogs.housing.transfer_money", new_callable=AsyncMock)
    @patch("amc_cogs.housing.extend_house_rent", new_callable=AsyncMock)
    @patch("amc_cogs.housing.record_treasury_rent_income", new_callable=AsyncMock)
    @patch("amc_cogs.housing.register_player_withdrawal", new_callable=AsyncMock)
    @patch("amc_cogs.housing.get_player_bank_balance", new_callable=AsyncMock)
    async def test_rebate_discount_only_no_cashback(
        self,
        mock_balance,
        mock_withdrawal,
        mock_treasury,
        mock_extend,
        mock_transfer,
    ):
        """Rebate should reduce cost but NOT send money back to player.

        Previously the code would charge net_cost AND then send the rebate
        amount back via transfer_money + send_fund_to_player_wallet,
        effectively paying players to extend their rent.
        """
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        mock_balance.return_value = Decimal("5000")

        view = self._make_view(
            character, player, rent_cost=1000, rebate=800, net_cost=200
        )
        interaction = AsyncMock()
        await view.confirm.callback(interaction)

        # Should charge the discounted amount only
        mock_withdrawal.assert_called_once_with(200, character, player)
        # Should record the discounted amount as treasury income
        mock_treasury.assert_called_once_with(
            200, f"House Rent Extend — {character.guid}"
        )
        # Should extend the rent
        mock_extend.assert_called_once()
        # CRITICAL: transfer_money must NOT be called (no rebate cashback)
        mock_transfer.assert_not_called()

    @patch("amc_cogs.housing.transfer_money", new_callable=AsyncMock)
    @patch("amc_cogs.housing.extend_house_rent", new_callable=AsyncMock)
    @patch("amc_cogs.housing.record_treasury_rent_income", new_callable=AsyncMock)
    @patch("amc_cogs.housing.register_player_withdrawal", new_callable=AsyncMock)
    @patch("amc_cogs.housing.get_player_bank_balance", new_callable=AsyncMock)
    async def test_full_rebate_zero_cost(
        self,
        mock_balance,
        mock_withdrawal,
        mock_treasury,
        mock_extend,
        mock_transfer,
    ):
        """100% rebate → player pays nothing, receives nothing."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        mock_balance.return_value = Decimal("5000")

        view = self._make_view(
            character, player, rent_cost=1000, rebate=1000, net_cost=0
        )
        interaction = AsyncMock()
        await view.confirm.callback(interaction)

        mock_withdrawal.assert_not_called()
        mock_treasury.assert_not_called()
        mock_extend.assert_called_once()
        mock_transfer.assert_not_called()

    @patch("amc_cogs.housing.transfer_money", new_callable=AsyncMock)
    @patch("amc_cogs.housing.extend_house_rent", new_callable=AsyncMock)
    @patch("amc_cogs.housing.record_treasury_rent_income", new_callable=AsyncMock)
    @patch("amc_cogs.housing.register_player_withdrawal", new_callable=AsyncMock)
    @patch("amc_cogs.housing.get_player_bank_balance", new_callable=AsyncMock)
    async def test_no_rebate_full_cost(
        self,
        mock_balance,
        mock_withdrawal,
        mock_treasury,
        mock_extend,
        mock_transfer,
    ):
        """No rebate → full cost charged, no cashback."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        mock_balance.return_value = Decimal("5000")

        view = self._make_view(
            character, player, rent_cost=1000, rebate=0, net_cost=1000
        )
        interaction = AsyncMock()
        await view.confirm.callback(interaction)

        mock_withdrawal.assert_called_once_with(1000, character, player)
        mock_treasury.assert_called_once_with(
            1000, f"House Rent Extend — {character.guid}"
        )
        mock_extend.assert_called_once()
        mock_transfer.assert_not_called()

    @patch("amc_cogs.housing.extend_house_rent", new_callable=AsyncMock)
    @patch("amc_cogs.housing.register_player_withdrawal", new_callable=AsyncMock)
    @patch("amc_cogs.housing.get_player_bank_balance", new_callable=AsyncMock)
    async def test_insufficient_balance_blocks_extension(
        self, mock_balance, mock_withdrawal, mock_extend
    ):
        """Should not proceed if balance < net_cost."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        mock_balance.return_value = Decimal("100")

        view = self._make_view(
            character, player, rent_cost=1000, rebate=800, net_cost=200
        )
        interaction = AsyncMock()
        await view.confirm.callback(interaction)

        mock_withdrawal.assert_not_called()
        mock_extend.assert_not_called()
        interaction.followup.send.assert_called_once()
        msg = interaction.followup.send.call_args[0][0]
        self.assertIn("Insufficient", msg)

    @patch("amc_cogs.housing.extend_house_rent", new_callable=AsyncMock)
    @patch(
        "amc_cogs.housing.register_player_withdrawal",
        new_callable=AsyncMock,
        side_effect=ValueError("insufficient funds"),
    )
    @patch("amc_cogs.housing.get_player_bank_balance", new_callable=AsyncMock)
    async def test_bank_error_blocks_extension(
        self, mock_balance, mock_withdrawal, mock_extend
    ):
        """Should not proceed if bank withdrawal raises ValueError."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        mock_balance.return_value = Decimal("5000")

        view = self._make_view(
            character, player, rent_cost=1000, rebate=800, net_cost=200
        )
        interaction = AsyncMock()
        await view.confirm.callback(interaction)

        mock_extend.assert_not_called()
        msg = interaction.followup.send.call_args[0][0]
        self.assertIn("Bank error", msg)

    @patch(
        "amc_cogs.housing.extend_house_rent",
        new_callable=AsyncMock,
        side_effect=Exception("API down"),
    )
    @patch("amc_cogs.housing.record_treasury_rent_income", new_callable=AsyncMock)
    @patch("amc_cogs.housing.register_player_withdrawal", new_callable=AsyncMock)
    @patch("amc_cogs.housing.get_player_bank_balance", new_callable=AsyncMock)
    async def test_extend_failure_no_treasury_record(
        self, mock_balance, mock_withdrawal, mock_treasury, mock_extend
    ):
        """If extend_house_rent fails, treasury should not record income."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        mock_balance.return_value = Decimal("5000")

        view = self._make_view(
            character, player, rent_cost=1000, rebate=800, net_cost=200
        )
        interaction = AsyncMock()
        await view.confirm.callback(interaction)

        mock_withdrawal.assert_called_once()  # already happened before extend
        mock_treasury.assert_not_called()  # not reached
        msg = interaction.followup.send.call_args[0][0]
        self.assertIn("Failed", msg)

    @patch("amc_cogs.housing.transfer_money", new_callable=AsyncMock)
    @patch("amc_cogs.housing.extend_house_rent", new_callable=AsyncMock)
    @patch("amc_cogs.housing.record_treasury_rent_income", new_callable=AsyncMock)
    @patch("amc_cogs.housing.register_player_withdrawal", new_callable=AsyncMock)
    @patch("amc_cogs.housing.get_player_bank_balance", new_callable=AsyncMock)
    async def test_market_breakdown_shown_in_embed(
        self,
        mock_balance,
        mock_withdrawal,
        mock_treasury,
        mock_extend,
        mock_transfer,
    ):
        """Market breakdown should be shown in the confirm embed."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        mock_balance.return_value = Decimal("5000")

        breakdown = {
            "avg_players": 15.0,
            "player_factor": 1.5,
            "avg_rent_pct": 80.0,
            "rent_health": 1.0,
            "multiplier": 1.5,
        }
        view = ExtendConfirmView(
            character=character,
            player=player,
            house_guid="test-house-guid",
            house_key="TestHouse_01",
            extend_seconds=86400,
            extend_days=1,
            rent_cost=1500,
            rebate=0,
            net_cost=1500,
            mod_session=AsyncMock(),
            market_breakdown=breakdown,
        )
        interaction = AsyncMock()
        await view.confirm.callback(interaction)

        embed = interaction.followup.send.call_args[1]["embed"]
        market_field = next(f for f in embed.fields if f.name == "Market Rate")
        self.assertIn("1.5x", market_field.value)
        self.assertIn("players 1.5", market_field.value)
        self.assertIn("rent health 1.0", market_field.value)


class HandleExtendRebateCalculationTests(TestCase):
    """Tests for rebate calculation in CharacterSelect._handle_extend.

    The formula:
        cost_per_day = int(cost * ratio / max_days)
        rent_cost    = cost_per_day * int(extend_days)
        market_rent_cost = int(rent_cost * market_multiplier)
        effective_cost = int(market_rent_cost * license.rebate_pct / 100)  [if license]
                         market_rent_cost                                  [if no license]
        rebate    = min(total_earnings, effective_cost)
        net_cost  = max(0, market_rent_cost − rebate)
    """

    @patch("amc_cogs.housing.get_market_multiplier", return_value=(1.0, DEFAULT_MARKET_BREAKDOWN))
    @patch("amc_cogs.housing.get_player_bank_balance", new_callable=AsyncMock)
    @patch("amc_cogs.housing.is_player_online", new_callable=AsyncMock, return_value=True)
    async def test_rebate_capped_at_total_earnings(self, mock_online, mock_balance, mock_market):
        """Rebate should not exceed total delivery earnings."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="a" * 32
        )

        # 5 deliveries × 100 payment = 500 total earnings
        now = timezone.now()
        for i in range(5):
            await Delivery.objects.acreate(
                character=character,
                timestamp=now - timedelta(days=i),
                cargo_key="SmallBox",
                quantity=1,
                payment=100,
                subsidy=0,
            )

        mock_balance.return_value = Decimal("10000")

        house_data = {
            "HouseGuid": "house-123",
            "HousegKey": "TestHouse_01",
            "Net_OwnerCharacterGuid": character.guid,
            "Net_RentLeftTimeSeconds": 5 * 86400,  # 5 days remaining
        }
        rent_info = {
            "Cost": 1000,
            "HousingPlotRentalPriceRatio": 5.0,
            "MaxHousingPlotRentalDays": 15,
        }

        select = CharacterSelect(
            characters=[character],
            action="extend",
            house_data=house_data,
            rent_info=rent_info,
            houses=[house_data],
        )
        interaction = AsyncMock()
        interaction.client.http_client_mod = AsyncMock()
        interaction.client.http_client_game = AsyncMock()

        await select._handle_extend(interaction, character)

        view = interaction.followup.send.call_args[1]["view"]
        # cost_per_day = int(1000 * 5.0 / 15) = 333
        # extend_days  = 15 − 5 = 10
        # rent_cost    = 333 * 10 = 3330
        # market_multiplier = 1.0 → market_rent_cost = 3330
        # No license → effective_cost = 3330
        # rebate   = min(500, 3330) = 500   (capped at earnings)
        # net_cost = 3330 − 500 = 2830
        self.assertEqual(view.rent_cost, 3330)
        self.assertEqual(view.rebate, 500)
        self.assertEqual(view.net_cost, 2830)

    @patch("amc_cogs.housing.get_market_multiplier", return_value=(1.0, DEFAULT_MARKET_BREAKDOWN))
    @patch("amc_cogs.housing.get_player_bank_balance", new_callable=AsyncMock)
    @patch("amc_cogs.housing.is_player_online", new_callable=AsyncMock, return_value=True)
    async def test_rebate_capped_at_license_percentage(self, mock_online, mock_balance, mock_market):
        """Rebate capped by license percentage even with excess earnings."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="b" * 32
        )

        # 50% license (applies to all houses)
        await HousingLicense.objects.acreate(
            character=character, house_key=None, rebate_pct=Decimal("50.00")
        )

        # Large earnings — should be irrelevant since license caps it
        now = timezone.now()
        for i in range(100):
            await Delivery.objects.acreate(
                character=character,
                timestamp=now - timedelta(days=i % 30),
                cargo_key="SmallBox",
                quantity=1,
                payment=1000,
                subsidy=0,
            )

        mock_balance.return_value = Decimal("100000")

        house_data = {
            "HouseGuid": "house-456",
            "HousegKey": "TestHouse_02",
            "Net_OwnerCharacterGuid": character.guid,
            "Net_RentLeftTimeSeconds": 5 * 86400,
        }
        rent_info = {
            "Cost": 1000,
            "HousingPlotRentalPriceRatio": 5.0,
            "MaxHousingPlotRentalDays": 15,
        }

        select = CharacterSelect(
            characters=[character],
            action="extend",
            house_data=house_data,
            rent_info=rent_info,
            houses=[house_data],
        )
        interaction = AsyncMock()
        interaction.client.http_client_mod = AsyncMock()
        interaction.client.http_client_game = AsyncMock()

        await select._handle_extend(interaction, character)

        view = interaction.followup.send.call_args[1]["view"]
        # rent_cost    = 333 * 10 = 3330
        # market_multiplier = 1.0 → market_rent_cost = 3330
        # effective_cost = int(3330 * 50 / 100) = 1665
        # rebate   = min(100000, 1665) = 1665  (capped by license)
        # net_cost = 3330 − 1665 = 1665
        self.assertEqual(view.rent_cost, 3330)
        self.assertEqual(view.rebate, 1665)
        self.assertEqual(view.net_cost, 1665)

    @patch("amc_cogs.housing.get_market_multiplier", return_value=(1.0, DEFAULT_MARKET_BREAKDOWN))
    @patch("amc_cogs.housing.get_player_bank_balance", new_callable=AsyncMock)
    @patch("amc_cogs.housing.is_player_online", new_callable=AsyncMock, return_value=True)
    async def test_no_deliveries_zero_rebate(self, mock_online, mock_balance, mock_market):
        """No deliveries → rebate = 0 → player pays full rent_cost."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="d" * 32
        )

        mock_balance.return_value = Decimal("10000")

        house_data = {
            "HouseGuid": "house-abc",
            "HousegKey": "TestHouse_04",
            "Net_OwnerCharacterGuid": character.guid,
            "Net_RentLeftTimeSeconds": 5 * 86400,
        }
        rent_info = {
            "Cost": 1000,
            "HousingPlotRentalPriceRatio": 5.0,
            "MaxHousingPlotRentalDays": 15,
        }

        select = CharacterSelect(
            characters=[character],
            action="extend",
            house_data=house_data,
            rent_info=rent_info,
            houses=[house_data],
        )
        interaction = AsyncMock()
        interaction.client.http_client_mod = AsyncMock()
        interaction.client.http_client_game = AsyncMock()

        await select._handle_extend(interaction, character)

        view = interaction.followup.send.call_args[1]["view"]
        # rent_cost = 333 * 10 = 3330
        # market_multiplier = 1.0 → market_rent_cost = 3330
        # No deliveries → total_earnings = 0
        # rebate   = min(0, 3330) = 0
        # net_cost = 3330
        self.assertEqual(view.rebate, 0)
        self.assertEqual(view.net_cost, 3330)

    @patch("amc_cogs.housing.get_market_multiplier", return_value=(1.0, DEFAULT_MARKET_BREAKDOWN))
    @patch("amc_cogs.housing.get_player_bank_balance", new_callable=AsyncMock)
    @patch("amc_cogs.housing.is_player_online", new_callable=AsyncMock, return_value=True)
    async def test_earnings_cover_full_rent_no_license(self, mock_online, mock_balance, mock_market):
        """Enough earnings + no license → rebate covers full rent → net_cost = 0."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="e" * 32
        )

        # 50 deliveries × 100 = 5000 earnings (far exceeds rent_cost of 333)
        now = timezone.now()
        for i in range(50):
            await Delivery.objects.acreate(
                character=character,
                timestamp=now - timedelta(days=i % 30),
                cargo_key="SmallBox",
                quantity=1,
                payment=100,
                subsidy=0,
            )

        mock_balance.return_value = Decimal("100000")

        house_data = {
            "HouseGuid": "house-def",
            "HousegKey": "TestHouse_05",
            "Net_OwnerCharacterGuid": character.guid,
            "Net_RentLeftTimeSeconds": 14 * 86400,  # 14 days remaining
        }
        rent_info = {
            "Cost": 1000,
            "HousingPlotRentalPriceRatio": 5.0,
            "MaxHousingPlotRentalDays": 15,
        }

        select = CharacterSelect(
            characters=[character],
            action="extend",
            house_data=house_data,
            rent_info=rent_info,
            houses=[house_data],
        )
        interaction = AsyncMock()
        interaction.client.http_client_mod = AsyncMock()
        interaction.client.http_client_game = AsyncMock()

        await select._handle_extend(interaction, character)

        view = interaction.followup.send.call_args[1]["view"]
        # cost_per_day = 333
        # extend_days  = 15 − 14 = 1
        # rent_cost    = 333
        # market_multiplier = 1.0 → market_rent_cost = 333
        # total_earnings = 5000
        # No license → effective_cost = 333
        # rebate   = min(5000, 333) = 333
        # net_cost = 0
        self.assertEqual(view.rent_cost, 333)
        self.assertEqual(view.rebate, 333)
        self.assertEqual(view.net_cost, 0)

    @patch("amc_cogs.housing.get_market_multiplier", return_value=(1.0, DEFAULT_MARKET_BREAKDOWN))
    @patch("amc_cogs.housing.get_player_bank_balance", new_callable=AsyncMock)
    @patch("amc_cogs.housing.is_player_online", new_callable=AsyncMock, return_value=True)
    async def test_house_specific_license_preferred(self, mock_online, mock_balance, mock_market):
        """House-specific license should be preferred over general license."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="f" * 32
        )

        house_guid = "house-789"

        # General license: 20%
        await HousingLicense.objects.acreate(
            character=character, house_key=None, rebate_pct=Decimal("20.00")
        )
        # House-specific license: 80%
        await HousingLicense.objects.acreate(
            character=character, house_key=house_guid, rebate_pct=Decimal("80.00")
        )

        # Enough earnings that the license % is the binding constraint
        now = timezone.now()
        for i in range(100):
            await Delivery.objects.acreate(
                character=character,
                timestamp=now - timedelta(days=i % 30),
                cargo_key="SmallBox",
                quantity=1,
                payment=1000,
                subsidy=0,
            )

        mock_balance.return_value = Decimal("100000")

        house_data = {
            "HouseGuid": house_guid,
            "HousegKey": "TestHouse_06",
            "Net_OwnerCharacterGuid": character.guid,
            "Net_RentLeftTimeSeconds": 5 * 86400,
        }
        rent_info = {
            "Cost": 1000,
            "HousingPlotRentalPriceRatio": 5.0,
            "MaxHousingPlotRentalDays": 15,
        }

        select = CharacterSelect(
            characters=[character],
            action="extend",
            house_data=house_data,
            rent_info=rent_info,
            houses=[house_data],
        )
        interaction = AsyncMock()
        interaction.client.http_client_mod = AsyncMock()
        interaction.client.http_client_game = AsyncMock()

        await select._handle_extend(interaction, character)

        view = interaction.followup.send.call_args[1]["view"]
        # rent_cost      = 333 * 10 = 3330
        # market_multiplier = 1.0 → market_rent_cost = 3330
        # effective_cost = int(3330 * 80 / 100) = 2664  (80% from house-specific license)
        # rebate         = min(100000, 2664) = 2664
        # net_cost       = 3330 − 2664 = 666
        self.assertEqual(view.rent_cost, 3330)
        self.assertEqual(view.rebate, 2664)
        self.assertEqual(view.net_cost, 666)


class MarketMultiplierTests(TestCase):
    """Tests for get_market_multiplier — the dynamic pricing engine."""

    async def test_no_player_data_returns_base(self):
        """With no ServerStatus records, avg_players = 0 → min player_factor."""
        from django.core.cache import cache

        cache.clear()
        houses = [
            {"Net_OwnerCharacterGuid": "a" * 32, "Net_RentLeftTimeSeconds": 10 * 86400},
            {"Net_OwnerCharacterGuid": "b" * 32, "Net_RentLeftTimeSeconds": 10 * 86400},
        ]
        multiplier, breakdown = await get_market_multiplier(houses, 15)
        # avg_players = 0, ref = 10 → player_factor = clamp(0/10, 0.5, 2.0) = 0.5
        # avg_rent_pct = 10/15 = 0.667 → rent_health = clamp(0.667, 0.5, 1.5) = 0.667 (rounded 0.67)
        # raw = 0.5 * 0.667 = 0.333 → multiplier = clamp(0.333, 0.5, 2.0) = 0.5
        self.assertEqual(breakdown["player_factor"], 0.5)
        self.assertEqual(multiplier, 0.5)

    @override_settings(HOUSING_MARKET_REFERENCE_PLAYERS=10)
    async def test_high_player_count_increases_multiplier(self):
        """High avg player count → player_factor > 1 → multiplier > 1."""
        from django.core.cache import cache

        cache.clear()
        now = timezone.now()
        for i in range(50):
            await ServerStatus.objects.acreate(
                timestamp=now - timedelta(hours=i),
                num_players=20,
                fps=60,
                used_memory=1024,
            )

        houses = [
            {"Net_OwnerCharacterGuid": "a" * 32, "Net_RentLeftTimeSeconds": 14 * 86400},
        ]
        multiplier, breakdown = await get_market_multiplier(houses, 15)
        # avg_players = 20, ref = 10 → player_factor = clamp(20/10, 0.5, 2.0) = 2.0
        # avg_rent_pct = 14/15 = 0.933 → rent_health = clamp(0.933, 0.5, 1.5) = 0.93 (rounded)
        # raw = 2.0 * 0.933 = 1.867 → multiplier = clamp(1.867, 0.5, 2.0) = 1.87 (rounded)
        self.assertEqual(breakdown["player_factor"], 2.0)
        self.assertGreater(multiplier, 1.5)

    async def test_low_rent_health_reduces_multiplier(self):
        """Most houses nearly expired → low rent_health → reduced multiplier."""
        from django.core.cache import cache

        cache.clear()
        now = timezone.now()
        for i in range(10):
            await ServerStatus.objects.acreate(
                timestamp=now - timedelta(hours=i),
                num_players=10,
                fps=60,
                used_memory=1024,
            )

        houses = [
            {"Net_OwnerCharacterGuid": "a" * 32, "Net_RentLeftTimeSeconds": 2 * 86400},
            {"Net_OwnerCharacterGuid": "b" * 32, "Net_RentLeftTimeSeconds": 1 * 86400},
        ]
        multiplier, breakdown = await get_market_multiplier(houses, 15)
        # avg_players = 10, ref = 10 → player_factor = 1.0
        # avg_rent_pct = ((2/15) + (1/15)) / 2 = 0.1 → rent_health = clamp(0.1, 0.5, 1.5) = 0.5
        # raw = 1.0 * 0.5 = 0.5 → multiplier = 0.5
        self.assertEqual(breakdown["player_factor"], 1.0)
        self.assertEqual(breakdown["rent_health"], 0.5)
        self.assertEqual(multiplier, 0.5)

    async def test_unrented_houses_excluded(self):
        """Empty/unrented houses should not affect rent health calculation."""
        from django.core.cache import cache

        cache.clear()
        now = timezone.now()
        for i in range(10):
            await ServerStatus.objects.acreate(
                timestamp=now - timedelta(hours=i),
                num_players=10,
                fps=60,
                used_memory=1024,
            )

        houses = [
            {"Net_OwnerCharacterGuid": "", "Net_RentLeftTimeSeconds": 0},
            {"Net_OwnerCharacterGuid": "00000000000000000000000000000000", "Net_RentLeftTimeSeconds": 0},
            {"Net_OwnerCharacterGuid": "a" * 32, "Net_RentLeftTimeSeconds": 10 * 86400},
        ]
        multiplier, breakdown = await get_market_multiplier(houses, 15)
        # Only 1 rented house: 10/15 = 0.667 → rent_health = 0.67
        self.assertEqual(breakdown["avg_rent_pct"], 66.7)

    async def test_multiplier_applied_to_rent_cost(self):
        """Market multiplier should scale rent_cost in _handle_extend."""
        from django.core.cache import cache

        cache.clear()
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, guid="g" * 32
        )

        mock_breakdown = {
            "avg_players": 20.0,
            "player_factor": 2.0,
            "avg_rent_pct": 93.3,
            "rent_health": 0.93,
            "multiplier": 1.87,
        }

        with patch(
            "amc_cogs.housing.get_market_multiplier",
            return_value=(1.87, mock_breakdown),
        ):
            with patch(
                "amc_cogs.housing.get_player_bank_balance",
                new_callable=AsyncMock,
                return_value=Decimal("100000"),
            ):
                with patch(
                    "amc_cogs.housing.is_player_online",
                    new_callable=AsyncMock,
                    return_value=True,
                ):
                    house_data = {
                        "HouseGuid": "house-market",
                        "HousegKey": "TestHouse_Market",
                        "Net_OwnerCharacterGuid": character.guid,
                        "Net_RentLeftTimeSeconds": 5 * 86400,
                    }
                    rent_info = {
                        "Cost": 1000,
                        "HousingPlotRentalPriceRatio": 5.0,
                        "MaxHousingPlotRentalDays": 15,
                    }

                    select = CharacterSelect(
                        characters=[character],
                        action="extend",
                        house_data=house_data,
                        rent_info=rent_info,
                        houses=[house_data],
                    )
                    interaction = AsyncMock()
                    interaction.client.http_client_mod = AsyncMock()
                    interaction.client.http_client_game = AsyncMock()

                    await select._handle_extend(interaction, character)

                    view = interaction.followup.send.call_args[1]["view"]
                    # cost_per_day = int(1000 * 5.0 / 15) = 333
                    # extend_days  = 10
                    # base_rent_cost = 333 * 10 = 3330
                    # market_rent_cost = int(3330 * 1.87) = 6227
                    self.assertEqual(view.rent_cost, 6227)
                    self.assertEqual(view.net_cost, 6227)

    async def test_cache_returns_consistent_results(self):
        """Second call within cache window should return cached value."""
        from django.core.cache import cache

        cache.clear()
        now = timezone.now()
        for i in range(10):
            await ServerStatus.objects.acreate(
                timestamp=now - timedelta(hours=i),
                num_players=15,
                fps=60,
                used_memory=1024,
            )

        houses = [
            {"Net_OwnerCharacterGuid": "a" * 32, "Net_RentLeftTimeSeconds": 10 * 86400},
        ]
        m1, b1 = await get_market_multiplier(houses, 15, cache_timeout=3600)
        m2, b2 = await get_market_multiplier(houses, 15, cache_timeout=3600)
        self.assertEqual(m1, m2)
        self.assertEqual(b1, b2)
