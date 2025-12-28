from django.test import TestCase
from unittest.mock import AsyncMock, MagicMock
from datetime import timedelta
from django.utils import timezone
from amc_cogs.moderation import ModerationCog
from amc.models import Player, Character, Delivery, PlayerChatLog, PlayerStatusLog, Ticket, Team, TeamMembership
from amc_finance.models import Account
from psycopg.types.range import Range

class ModerationCogTestCase(TestCase):
    def setUp(self):
        # Mock bot instance
        self.bot = MagicMock()
        self.bot.http_client_game = MagicMock()
        self.bot.http_client_mod = MagicMock()
        self.bot.event_http_client_mod = MagicMock()
        self.bot.event_http_client_game = MagicMock()
        
        # Create cog
        self.cog = ModerationCog(self.bot)
        
        # Mock interaction context
        self.ctx = MagicMock()
        self.ctx.response = AsyncMock()
        self.ctx.followup = AsyncMock()
        self.ctx.user = MagicMock()
        self.ctx.user.id = 123456789
        self.ctx.user.display_name = "TestAdmin"

    async def test_profile_player_comprehensive(self):
        """Test /admin profile command with all sections and metrics"""
        now = timezone.now()
        
        # 1. Create Player with flags and notes
        player = await Player.objects.acreate(
            unique_id=76561198000000001,
            discord_user_id=88888888,
            discord_name="DiscordUser",
            social_score=42,
            adminstrator=True,
            suspect=True,
            displayer=True,
            notes="Always brings pizza."
        )
        
        # 2. Create Character with levels and RP mode
        char = await Character.objects.acreate(
            player=player,
            name="MainChar",
            guid="guid-main",
            money=1337,
            driver_level=10,
            truck_level=5,
            rp_mode=True
        )
        
        # 3. Create Bank Account for character
        await Account.objects.acreate(
            character=char,
            book=Account.Book.BANK,
            account_type=Account.AccountType.ASSET,
            name="Main Savings",
            balance=50000.00
        )
        
        # 4. Activity Logs (First seen, Last online, Session time)
        await PlayerStatusLog.objects.acreate(
            character=char,
            timespan=Range(now - timedelta(days=10, hours=4), now - timedelta(days=10))
        )
        await PlayerStatusLog.objects.acreate(
            character=char,
            timespan=Range(now - timedelta(hours=2), now - timedelta(hours=1)) # Recent session
        )
        
        # 5. Economy (Deliveries, Revenue)
        await Delivery.objects.acreate(
            character=char,
            payment=1000,
            subsidy=500,
            quantity=1,
            timestamp=now - timedelta(days=1)
        )
        
        # 6. Infractions (Tickets)
        await Ticket.objects.acreate(
            player=player,
            infringement=Ticket.Infringement.NUISANCE,
            created_at=now - timedelta(days=2)
        )
        
        # 7. Teams
        team = await Team.objects.acreate(
            name="TestTeam",
            tag="TT",
            discord_thread_id=123
        )
        await TeamMembership.objects.acreate(
            player=player,
            character=char,
            team=team
        )
        
        # 8. Chat Logs
        await PlayerChatLog.objects.acreate(
            character=char,
            text="Howdy partner",
            timestamp=now - timedelta(minutes=10)
        )

        # Execute Command
        await self.cog.profile_player.callback(self.cog, self.ctx, str(player.unique_id))
        
        # Verification
        self.ctx.followup.send.assert_called_once()
        embed = self.ctx.followup.send.call_args.kwargs['embed']
        
        # Identity Checks
        self.assertIn("Verified", embed.fields[0].value)
        self.assertIn("Admin", embed.fields[0].value)
        self.assertIn("Suspect", embed.fields[0].value)
        self.assertIn("Displayer", embed.fields[0].value)
        self.assertIn("Always brings pizza", embed.fields[0].value)
        self.assertIn("42", embed.fields[0].value)
        self.assertIn("<@88888888>", embed.fields[0].value)
        
        # Characters Checks
        self.assertIn("MainChar", embed.fields[1].value)
        self.assertIn("Wallet: `$1,337`", embed.fields[1].value)
        self.assertIn("Bank: `$50,000`", embed.fields[1].value)
        self.assertIn("D:10 | T:5", embed.fields[1].value)
        self.assertIn("(RP)", embed.fields[1].value)
        
        # Activity Checks
        self.assertIn("Total online:", embed.fields[2].value)
        self.assertIn("5h", embed.fields[2].value)
        self.assertIn("1h", embed.fields[2].value) # Recent (7d) session time
        
        # Economy Checks
        self.assertIn("**Deliveries:** `1`", embed.fields[3].value)
        self.assertIn("**Revenue:** `$1,500`", embed.fields[3].value)
        self.assertIn("**Avg/Job:** `$1,500`", embed.fields[3].value)
        self.assertIn("**Messages:** `1`", embed.fields[3].value)

        # Infractions Checks
        self.assertIn("Total Tickets:** `1`", embed.fields[4].value)
        self.assertIn("Public Nuisance", embed.fields[4].value)

        # Teams Checks
        self.assertIn("TestTeam", embed.fields[5].value)

    async def test_profile_player_not_found(self):
        """Test /admin profile with non-existent player"""
        await self.cog.profile_player.callback(self.cog, self.ctx, "99999999999")
        args = self.ctx.followup.send.call_args[0]
        self.assertIn("Player not found", args[0])
