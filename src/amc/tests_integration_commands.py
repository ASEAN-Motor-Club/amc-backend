from django.test import TestCase
from unittest.mock import AsyncMock, patch, MagicMock
from amc import tasks
from amc.models import Character, Player
from amc.server_logs import PlayerChatMessageLogEvent
from django.utils import timezone

class IntegrationCommandWorkflowTest(TestCase):
    async def test_tasks_route_command(self):
        """
        Integration test to ensure tasks.py -> registry -> commands package works.
        We simulate a log line "/help". 
        If 'amc.commands' is correctly imported by tasks.py, standard commands will be registered.
        We mock CommandContext to verify the command handler calls ctx.reply().
        """
        # 1. Setup Data
        player_id = 123456789
        player_name = "TestPlayer"
        character_guid = "test-guid"
        timestamp = timezone.now()
        
        mock_char = MagicMock(spec=Character)
        mock_char.guid = character_guid
        mock_char.name = player_name
        mock_player = MagicMock(spec=Player)
        mock_player.unique_id = player_id
        
        # 2. Mock Dependencies
        # We mock CommandContext so we can capture the 'reply' call made by the real cmd_help.
        mock_ctx_class = MagicMock()
        mock_ctx_instance = mock_ctx_class.return_value
        mock_ctx_instance.reply = AsyncMock()
        mock_ctx_instance.character = mock_char
        mock_ctx_instance.player = mock_player
        
        with patch('amc.tasks.aget_or_create_character', new=AsyncMock(return_value=(mock_char, mock_player, False, {}))), \
             patch('amc.command_framework.CommandContext', mock_ctx_class), \
             patch('amc.models.PlayerChatLog.objects.acreate', new=AsyncMock()), \
             patch('amc.models.BotInvocationLog.objects.acreate', new=AsyncMock()):
            
            # 3. Simulate Log Event
            log_event = PlayerChatMessageLogEvent(timestamp, player_name, str(player_id), "/help")
            
            ctx = {
                'http_client': MagicMock(),
                'http_client_mod': MagicMock(),
                'discord_client': MagicMock(),
            }
            
            # 4. Execute
            await tasks.process_log_event(log_event, ctx=ctx)
            
            # 5. Verify
            # If the command was found and executed:
            # 1. tasks.py created CommandContext (mock_ctx_class called)
            # 2. registry.execute called real cmd_help
            # 3. real cmd_help called ctx.reply(...)
            mock_ctx_instance.reply.assert_called()
            
            # Verify it was indeed help output
            args, _ = mock_ctx_instance.reply.call_args
            self.assertIn("Available Commands", args[0])
