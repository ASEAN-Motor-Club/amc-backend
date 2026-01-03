"""Tests for the bot_events SSE endpoint."""

import asyncio
import json
from django.test import TestCase
from amc.api.bot_events import emit_bot_event, _bot_event_queue


class BotEventsQueueTest(TestCase):
    """Tests for the bot_events event queue and emit functionality."""

    def setUp(self):
        # Clear the queue before each test
        while not _bot_event_queue.empty():
            try:
                _bot_event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def test_emit_bot_event_adds_to_queue(self):
        """Test that emit_bot_event correctly adds events to the queue."""
        event = {
            "type": "chat_message",
            "player_name": "TestPlayer",
            "message": "Hello world",
        }
        
        await emit_bot_event(event)
        
        # Verify event was added to queue
        self.assertFalse(_bot_event_queue.empty())
        queued_event = await _bot_event_queue.get()
        self.assertEqual(queued_event, event)

    async def test_emit_multiple_events_maintains_order(self):
        """Test that multiple events are queued in FIFO order."""
        events = [
            {"type": "chat_message", "message": "First"},
            {"type": "chat_message", "message": "Second"},
            {"type": "chat_message", "message": "Third"},
        ]
        
        for event in events:
            await emit_bot_event(event)
        
        # Verify FIFO order
        for expected in events:
            queued = await _bot_event_queue.get()
            self.assertEqual(queued["message"], expected["message"])

    async def test_event_contains_required_fields(self):
        """Test that events can contain all required bot event fields."""
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
        
        queued_event = await _bot_event_queue.get()
        self.assertEqual(queued_event["type"], "chat_message")
        self.assertEqual(queued_event["player_name"], "TestPlayer")
        self.assertEqual(queued_event["is_bot_command"], True)
        self.assertEqual(queued_event["discord_id"], "987654321")

    async def test_event_serializable_to_json(self):
        """Test that queued events can be serialized to JSON for SSE."""
        event = {
            "type": "chat_message",
            "player_name": "TestPlayer",
            "message": "Test message",
        }
        
        await emit_bot_event(event)
        queued_event = await _bot_event_queue.get()
        
        # Should be JSON serializable for SSE output
        json_str = json.dumps(queued_event)
        self.assertIn("chat_message", json_str)
        self.assertIn("TestPlayer", json_str)
