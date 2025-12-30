import discord
from django.test import TransactionTestCase
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import timedelta
from django.utils import timezone
from amc_cogs.leaderboard import LeaderboardCog
from amc.models import Player, Character, Delivery, PlayerVehicleLog, PlayerStatusLog, PlayerRestockDepotLog, Vehicle
from amc.enums import CargoKey
from psycopg.types.range import Range

class LeaderboardCogTestCase(TransactionTestCase):
    def setUp(self):
        # Mock bot instance
        self.bot = MagicMock()
        self.bot.user = MagicMock()
        self.bot.user.id = 12345
        self.bot.wait_until_ready = AsyncMock()
        
        # Patch tasks.loop.start to avoid "no running event loop" error
        with patch('discord.ext.tasks.Loop.start'):
            self.cog = LeaderboardCog(self.bot)

    async def test_get_leaderboard_data_basic(self):
        """Test that get_leaderboard_data returns data without crashing"""
        now = timezone.now()
        
        # Setup data
        player = await Player.objects.acreate(unique_id=1, discord_user_id=101)
        char = await Character.objects.acreate(player=player, name="Leader")
        
        # 1. Revenue
        await Delivery.objects.acreate(
            timestamp=now - timedelta(hours=2),
            character=char,
            cargo_key=CargoKey.AppleBox,
            quantity=10,
            payment=1000,
            subsidy=500
        )
        
        # 2. Vehicles
        vehicle = await Vehicle.objects.acreate(id=1, name="Cool Car")
        await PlayerVehicleLog.objects.acreate(
            timestamp=now - timedelta(hours=3),
            character=char,
            vehicle=vehicle,
            action=PlayerVehicleLog.Action.BOUGHT
        )
        
        # 3. Active Time
        await PlayerStatusLog.objects.acreate(
            character=char,
            timespan=Range(lower=now - timedelta(hours=5), upper=now - timedelta(hours=4))
        )
        
        # 4. Restocks
        await PlayerRestockDepotLog.objects.acreate(
            timestamp=now - timedelta(hours=1),
            character=char,
            depot_name="Main Depot"
        )
        
        data = await self.cog.get_leaderboard_data(1)
        
        self.assertEqual(len(data["revenue"]), 1)
        self.assertEqual(data["revenue"][0]["name"], "Leader")
        self.assertEqual(data["revenue"][0]["value"], 1500)
        
        self.assertEqual(len(data["vehicles"]), 1)
        self.assertEqual(data["vehicles"][0]["value"], 1)
        
        self.assertEqual(len(data["active"]), 1)
        self.assertAlmostEqual(float(data["active"][0]["value"]), 1.0)
        
        self.assertEqual(len(data["restocks"]), 1)
        self.assertEqual(data["restocks"][0]["value"], 1)

    async def test_get_leaderboard_data_null_names(self):
        """Test that get_leaderboard_data handles NULL character names"""
        now = timezone.now()
        
        # Setup data with missing character name
        player = await Player.objects.acreate(unique_id=2, discord_user_id=102)
        char = await Character.objects.acreate(player=player, name="") # Empty name
        
        await Delivery.objects.acreate(
            timestamp=now - timedelta(minutes=5),
            character=char,
            cargo_key=CargoKey.AppleBox,
            quantity=1,
            payment=100,
            subsidy=50
        )
        
        data = await self.cog.get_leaderboard_data(1)
        self.assertEqual(data["revenue"][0]["name"], "Unknown")
        self.assertEqual(data["revenue"][0]["value"], 150)

    async def test_create_leaderboard_embeds(self):
        """Test that create_leaderboard_embeds doesn't crash"""
        embed = await self.cog.create_leaderboard_embeds()
        self.assertIsInstance(embed, discord.Embed)
        self.assertIsInstance(embed.title, str)
        self.assertIn("🏆 ASEAN Motor Club Leaderboards", embed.title)
