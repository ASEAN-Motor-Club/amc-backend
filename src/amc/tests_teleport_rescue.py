from django.test import TestCase
from unittest.mock import MagicMock, AsyncMock, patch
from amc.command_framework import CommandContext
from amc.commands.teleport import cmd_tp_name
from amc.models import RescueRequest, Character, Player
from django.contrib.gis.geos import Point

class TeleportRescueTestCase(TestCase):
    def setUp(self):
        self.ctx = MagicMock(spec=CommandContext)
        self.ctx.reply = AsyncMock()
        self.ctx.announce = AsyncMock()
        
        # Proper async mock for http client
        self.ctx.http_client_mod = MagicMock()
        self.ctx.http_client_mod.post = AsyncMock()
        self.ctx.http_client_mod.get = AsyncMock()

        self.ctx.player_info = {
            'bIsAdmin': False,
            'CustomDestinationAbsoluteLocation': {'X': 100, 'Y': 200, 'Z': 300}
        }
        
        self.player = Player.objects.create(unique_id="76561198000000000")
        self.character = Character.objects.create(
            name="TestChar",
            player=self.player,
            guid="guid-123"
        )
        self.requester = Character.objects.create(
            name="Requester",
            player=Player.objects.create(unique_id="76561198000000001"),
            guid="guid-456"
        )
        
        self.ctx.character = self.character
        self.ctx.player = self.player

    async def test_cmd_tp_rescue_success(self):
        """
        Test that responding to a rescue allows using /tp without admin privileges.
        This verifies the fix for the async context error by exercising the 
        path that reads rescue.character.name.
        """
        # Create a rescue request where our player is a responder
        rescue = await RescueRequest.objects.acreate(
            character=self.requester,
            message="Help me",
            location=Point(100, 200, 300)
        )
        await rescue.responders.aadd(self.player)
        
        with patch('amc.commands.teleport.list_player_vehicles', new=AsyncMock(return_value={})), \
             patch('amc.commands.teleport.teleport_player', new=AsyncMock()) as mock_tp:
            
            # Should succeed now
            await cmd_tp_name(self.ctx, "")
            
            # Verify we tried to teleport
            mock_tp.assert_called_once()
            # And argument 2 (location) should match our custom dest
            call_args = mock_tp.call_args
            self.assertEqual(call_args[0][2], {'X': 100, 'Y': 200, 'Z': 305}) # Z+5 logic
