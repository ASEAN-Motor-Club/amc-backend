"""Tests for the in-game event commands (amc.commands.events)."""

from asgiref.sync import sync_to_async
from django.test import TestCase

from amc.commands.events import resolve_stagger_delay
from amc.factories import ScheduledEventFactory
from amc.models import GameEvent


class ResolveStaggerDelayTests(TestCase):
    async def test_manual_arg_wins(self):
        self.assertEqual(await resolve_stagger_delay(None, 7), 7.0)

    async def test_scheduled_event_field_used_when_arg_omitted(self):
        scheduled_event = await sync_to_async(ScheduledEventFactory)(
            staggered_start_delay=15
        )
        game_event = await GameEvent.objects.acreate(
            name="e", guid="GUID1", state=1, scheduled_event=scheduled_event
        )
        self.assertEqual(await resolve_stagger_delay(game_event, None), 15.0)

    async def test_zero_field_means_unset(self):
        scheduled_event = await sync_to_async(ScheduledEventFactory)(
            staggered_start_delay=0
        )
        game_event = await GameEvent.objects.acreate(
            name="e", guid="GUID2", state=1, scheduled_event=scheduled_event
        )
        self.assertEqual(await resolve_stagger_delay(game_event, None), 20.0)

    async def test_default_without_scheduled_event(self):
        game_event = await GameEvent.objects.acreate(
            name="e", guid="GUID3", state=1, scheduled_event=None
        )
        self.assertEqual(await resolve_stagger_delay(game_event, None), 20.0)
