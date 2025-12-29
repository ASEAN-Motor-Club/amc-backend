import discord
from django.test import TestCase
from unittest.mock import AsyncMock, MagicMock
from datetime import timedelta
from django.utils import timezone
from typing import Any, cast
from amc_cogs.delivery_stats import DeliveryStatsCog
from amc.models import Player, Character, ServerCargoArrivedLog, PlayerStatusLog
from amc.enums import CargoKey
from psycopg.types.range import Range


class DeliveryStatsCogTestCase(TestCase):
    def setUp(self):
        # Mock bot instance
        self.bot = MagicMock()
        self.bot.http_client_game = MagicMock()

        # Create cog
        self.cog = DeliveryStatsCog(self.bot)

        # Mock interaction
        self.interaction = AsyncMock(spec=discord.Interaction)
        self.interaction.response = AsyncMock()
        self.interaction.followup = AsyncMock()
        self.interaction.user = MagicMock()
        self.interaction.user.id = 123456789
        self.interaction.user.display_name = "TestUser"

    async def test_delivery_stats_no_player_no_verification(self):
        """Test /delivery_stats without player parameter when user is not verified"""
        # User 123456789 has no Player record

        await cast(Any, self.cog.delivery_stats.callback)(self.cog, self.interaction)

        # Verify error message
        self.interaction.followup.send.assert_called_once()
        args = self.interaction.followup.send.call_args[0]
        self.assertIn("You need to be verified", args[0])

    async def test_delivery_stats_success_multiple_cargo(self):
        """Test /delivery_stats with multiple cargo types and correct aggregation"""
        now = timezone.now()

        # Create player linked to the mock user
        player = await Player.objects.acreate(
            unique_id=76561198000000001,
            discord_user_id=123456789,
            discord_name="TestUser",
        )
        char = await Character.objects.acreate(player=player, name="TestChar")
        await PlayerStatusLog.objects.acreate(
            character=char, timespan=Range(now - timedelta(hours=1), now)
        )

        # Create deliveries
        # 2x Apples
        await ServerCargoArrivedLog.objects.acreate(
            player=player,
            character=char,
            cargo_key=CargoKey.AppleBox,
            payment=1000,
            weight=100.0,
            timestamp=now - timedelta(days=1),
        )
        await ServerCargoArrivedLog.objects.acreate(
            player=player,
            character=char,
            cargo_key=CargoKey.AppleBox,
            payment=1500,
            weight=150.0,
            timestamp=now - timedelta(days=2),
        )
        # 1x Carrots
        await ServerCargoArrivedLog.objects.acreate(
            player=player,
            character=char,
            cargo_key=CargoKey.CarrotBox,
            payment=500,
            weight=50.0,
            timestamp=now - timedelta(days=3),
        )

        # Run command
        await cast(Any, self.cog.delivery_stats.callback)(self.cog, self.interaction)

        # Verify response
        self.interaction.followup.send.assert_called_once()
        embed = self.interaction.followup.send.call_args.kwargs["embed"]

        self.assertEqual(embed.title, "📦 Delivery Stats: TestChar")

        # Verify breakdown field
        breakdown = embed.fields[0].value
        self.assertIn("Apples", breakdown)
        self.assertIn("2 deliveries | $2,500 | 250.0 kg", breakdown)
        self.assertIn("Carrots", breakdown)
        self.assertIn("1 deliveries | $500 | 50.0 kg", breakdown)

        # Verify totals field
        totals = embed.fields[1].value
        self.assertIn("Total Deliveries:** 3", totals)
        self.assertIn("Total Payment:** $3,000", totals)
        self.assertIn("Total Weight:** 300.0 kg", totals)

    async def test_delivery_stats_date_filtering(self):
        """Test that /delivery_stats correctly filters by date range"""
        now = timezone.now()

        player = await Player.objects.acreate(unique_id=1001, discord_user_id=123456789)
        char = await Character.objects.acreate(player=player, name="DateTester")

        # Delivery inside 30 days (default)
        await ServerCargoArrivedLog.objects.acreate(
            player=player,
            character=char,
            cargo_key=CargoKey.AppleBox,
            payment=1000,
            weight=100.0,
            timestamp=now - timedelta(days=10),
        )

        # Delivery outside 30 days
        await ServerCargoArrivedLog.objects.acreate(
            player=player,
            character=char,
            cargo_key=CargoKey.AppleBox,
            payment=2000,
            weight=200.0,
            timestamp=now - timedelta(days=40),
        )

        # Run command (default 30 days)
        await cast(Any, self.cog.delivery_stats.callback)(self.cog, self.interaction)

        totals_default = (
            self.interaction.followup.send.call_args.kwargs["embed"].fields[1].value
        )
        self.assertIn("Total Deliveries:** 1", totals_default)
        self.assertIn("$1,000", totals_default)

        self.interaction.followup.send.reset_mock()

        # Run command with 50 days
        await cast(Any, self.cog.delivery_stats.callback)(
            self.cog, self.interaction, days=50
        )

        totals_extended = (
            self.interaction.followup.send.call_args.kwargs["embed"].fields[1].value
        )
        self.assertIn("Total Deliveries:** 2", totals_extended)
        self.assertIn("$3,000", totals_extended)

    async def test_delivery_stats_no_deliveries(self):
        """Test /delivery_stats for player with no records"""
        await Player.objects.acreate(unique_id=1002, discord_user_id=123456789)

        await cast(Any, self.cog.delivery_stats.callback)(self.cog, self.interaction)

        self.interaction.followup.send.assert_called_once()
        embed = self.interaction.followup.send.call_args.kwargs["embed"]
        self.assertIn("No deliveries found", embed.description)

    async def test_delivery_stats_specific_player(self):
        """Test /delivery_stats specifying a player ID"""
        now = timezone.now()

        # Other player
        other_player = await Player.objects.acreate(unique_id=777)
        char = await Character.objects.acreate(player=other_player, name="Lucky")
        now = timezone.now()
        await PlayerStatusLog.objects.acreate(
            character=char, timespan=Range(now - timedelta(hours=1), now)
        )
        await ServerCargoArrivedLog.objects.acreate(
            player=other_player,
            cargo_key=CargoKey.AppleBox,
            payment=777,
            weight=7.7,
            timestamp=now,
        )

        # Interaction from original user
        await cast(Any, self.cog.delivery_stats.callback)(
            self.cog, self.interaction, player="777"
        )

        embed = self.interaction.followup.send.call_args.kwargs["embed"]
        self.assertIn("Lucky", embed.title)
        self.assertIn("$777", embed.fields[1].value)

    async def test_delivery_stats_player_not_found(self):
        """Test /delivery_stats with invalid player ID"""
        await cast(Any, self.cog.delivery_stats.callback)(
            self.cog, self.interaction, player="999999"
        )

        args = self.interaction.followup.send.call_args[0]
        self.assertIn("not verified their account", args[0])
