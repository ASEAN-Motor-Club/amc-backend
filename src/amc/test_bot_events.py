"""Tests for the bot_events SSE endpoint."""

import json
from unittest.mock import AsyncMock, patch
from django.test import TestCase
from amc.api.bot_events import emit_bot_event, BOT_EVENTS_CHANNEL


class BotEventsRedisTest(TestCase):
    """Tests for the bot_events Redis pub/sub functionality."""

    @patch('amc.api.bot_events.aioredis')
    async def test_emit_bot_event_publishes_to_redis(self, mock_aioredis):
        """Test that emit_bot_event correctly publishes events to Redis."""
        mock_client = AsyncMock()
        mock_aioredis.from_url.return_value = mock_client
        
        event = {
            "type": "chat_message",
            "player_name": "TestPlayer",
            "message": "Hello world",
        }
        
        await emit_bot_event(event)
        
        # Verify Redis client was created and publish was called
        mock_aioredis.from_url.assert_called_once()
        mock_client.publish.assert_called_once_with(
            BOT_EVENTS_CHANNEL, 
            json.dumps(event)
        )
        mock_client.aclose.assert_called_once()

    @patch('amc.api.bot_events.aioredis')
    async def test_emit_multiple_events(self, mock_aioredis):
        """Test that multiple events are published correctly."""
        mock_client = AsyncMock()
        mock_aioredis.from_url.return_value = mock_client
        
        events = [
            {"type": "chat_message", "message": "First"},
            {"type": "chat_message", "message": "Second"},
        ]
        
        for event in events:
            await emit_bot_event(event)
        
        # Verify publish was called for each event
        self.assertEqual(mock_client.publish.call_count, 2)

    @patch('amc.api.bot_events.aioredis')
    async def test_emit_event_with_all_fields(self, mock_aioredis):
        """Test that events with all bot event fields are correctly serialized."""
        mock_client = AsyncMock()
        mock_aioredis.from_url.return_value = mock_client
        
        event = {
            "type": "chat_message",
            "timestamp": "2026-01-03T12:00:00+00:00",
            "player_name": "TestPlayer",
            "player_id": "12345",
            "discord_id": "987654321",
            "character_guid": "abcd1234",
            "message": "Hello bot",
            "is_bot_command": True,
        }
        
        await emit_bot_event(event)
        
        # Verify the event was serialized as JSON
        call_args = mock_client.publish.call_args[0]
        self.assertEqual(call_args[0], BOT_EVENTS_CHANNEL)
        published_data = json.loads(call_args[1])
        self.assertEqual(published_data["type"], "chat_message")
        self.assertEqual(published_data["player_name"], "TestPlayer")
        self.assertEqual(published_data["is_bot_command"], True)
