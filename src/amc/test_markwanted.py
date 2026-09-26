"""Tests for /markwanted — on-duty-police command with a 20m proximity gate
and a configurable TTL."""

from unittest.mock import AsyncMock, MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from amc.models import Character, Player


class MarkWantedTestCase(TestCase):
    """/markwanted: sets Character.marked_wanted_until = now + TTL (from the
    WantedSystemConfig singleton) when the caller is on-duty police within
    20m of the target. No wanted row, no suspect overlay."""

    def setUp(self):
        from amc.commands.police import cmd_markwanted

        self.cmd_markwanted = cmd_markwanted

        self.cop_player = Player.objects.create(unique_id="76561199000002001")
        self.cop_char = Character.objects.create(
            name="OfficerCool",
            player=self.cop_player,
            guid="cop-guid-mark",
        )

        self.target_player = Player.objects.create(unique_id="76561199000002002")
        self.target_char = Character.objects.create(
            name="MarkTarget",
            player=self.target_player,
            guid="target-guid-mark",
        )

        # Caller at origin, target 10m away (1000 units) — within 20m
        self.mock_players = [
            (
                "76561199000002001",
                {
                    "character_guid": "cop-guid-mark",
                    "name": "OfficerCool",
                    "location": "X=0 Y=0 Z=0",
                },
            ),
            (
                "76561199000002002",
                {
                    "character_guid": "target-guid-mark",
                    "name": "MarkTarget",
                    "location": "X=1000 Y=0 Z=0",
                },
            ),
        ]

        self.ctx = MagicMock()
        self.ctx.player = self.cop_player
        self.ctx.character = self.cop_char
        self.ctx.http_client = MagicMock()
        self.ctx.http_client_mod = MagicMock()
        self.ctx.reply = AsyncMock()
        self.ctx.announce = AsyncMock()

    async def _run(self, name="MarkTarget", on_duty=True):
        with (
            patch(
                "amc.commands.police.get_players",
                new=AsyncMock(return_value=self.mock_players),
            ),
            patch(
                "amc.commands.police.is_police", new=AsyncMock(return_value=on_duty)
            ),
        ):
            await self.cmd_markwanted(self.ctx, name)

    async def test_marks_target_within_proximity(self):
        before = timezone.now()
        await self._run()

        await self.target_char.arefresh_from_db()
        self.assertIsNotNone(self.target_char.marked_wanted_until)
        delta = (
            self.target_char.marked_wanted_until - before
        ).total_seconds()
        # default TTL is 60 minutes (allow scheduler slop)
        self.assertGreater(delta, 59 * 60)
        self.assertLessEqual(delta, 61 * 60)
        self.ctx.reply.assert_called_once()
        self.assertIn("Wanted Marked", self.ctx.reply.call_args[0][0])

    async def test_too_far_target_is_rejected(self):
        """Target beyond 20m (here 3km) → 'Too Far' popup, no flag."""
        self.mock_players[1] = (
            "76561199000002002",
            {
                "character_guid": "target-guid-mark",
                "name": "MarkTarget",
                "location": "X=300000 Y=0 Z=0",
            },
        )
        await self._run()

        self.ctx.reply.assert_called_once()
        self.assertIn("Too Far", self.ctx.reply.call_args[0][0])
        await self.target_char.arefresh_from_db()
        self.assertIsNone(self.target_char.marked_wanted_until)

    async def test_not_on_duty_is_silently_ignored(self):
        await self._run(on_duty=False)

        self.ctx.reply.assert_not_called()
        await self.target_char.arefresh_from_db()
        self.assertIsNone(self.target_char.marked_wanted_until)

    async def test_ttl_comes_from_config(self):
        from amc.models import WantedSystemConfig

        await WantedSystemConfig.objects.acreate(
            pk=1, markwanted_ttl_minutes=15
        )
        before = timezone.now()
        await self._run()

        await self.target_char.arefresh_from_db()
        delta = (
            self.target_char.marked_wanted_until - before
        ).total_seconds()
        self.assertGreater(delta, 14 * 60)
        self.assertLessEqual(delta, 16 * 60)

    async def test_cannot_mark_yourself(self):
        await self._run(name="OfficerCool")

        self.ctx.reply.assert_called_once()
        self.assertIn("cannot mark yourself", self.ctx.reply.call_args[0][0])
        await self.cop_char.arefresh_from_db()
        self.assertIsNone(self.cop_char.marked_wanted_until)

    async def test_unknown_player_replies_not_found(self):
        with (
            patch(
                "amc.commands.police.get_players",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "amc.commands.police.is_police", new=AsyncMock(return_value=True)
            ),
        ):
            await self.cmd_markwanted(self.ctx, "Ghost")

        self.ctx.reply.assert_called_once()
        self.assertIn("Player not found", self.ctx.reply.call_args[0][0])
