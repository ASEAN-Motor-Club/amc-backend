"""Tests for /markwanted — admin-only wanted mark with a configurable TTL."""

from unittest.mock import AsyncMock, MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from amc.models import Character, Player


class MarkWantedTestCase(TestCase):
    """/markwanted: sets Character.marked_wanted_until = now + TTL (from the
    WantedSystemConfig singleton). No wanted row, no suspect overlay."""

    def setUp(self):
        from amc.commands.police import cmd_markwanted

        self.cmd_markwanted = cmd_markwanted

        self.admin_player = Player.objects.create(unique_id="76561199000002001")
        self.admin_char = Character.objects.create(
            name="AdminCool",
            player=self.admin_player,
            guid="admin-guid-mark",
        )

        self.target_player = Player.objects.create(unique_id="76561199000002002")
        self.target_char = Character.objects.create(
            name="MarkTarget",
            player=self.target_player,
            guid="target-guid-mark",
        )

        self.mock_players = [
            (
                "76561199000002002",
                {
                    "character_guid": "target-guid-mark",
                    "name": "MarkTarget",
                    "location": "X=0 Y=0 Z=0",
                },
            )
        ]

        self.ctx = MagicMock()
        self.ctx.player = self.admin_player
        self.ctx.character = self.admin_char
        self.ctx.player_info = {"bIsAdmin": True}
        self.ctx.http_client = MagicMock()
        self.ctx.http_client_mod = MagicMock()
        self.ctx.reply = AsyncMock()
        self.ctx.announce = AsyncMock()

    async def _run(self, name="MarkTarget"):
        with patch(
            "amc.commands.police.get_players",
            new=AsyncMock(return_value=self.mock_players),
        ):
            await self.cmd_markwanted(self.ctx, name)

    async def test_marks_target_with_default_ttl(self):
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

    async def test_non_admin_is_silently_ignored(self):
        self.ctx.player_info = {"bIsAdmin": False}
        with patch(
            "amc.commands.police.get_players", new=AsyncMock()
        ) as mock_get_players:
            await self.cmd_markwanted(self.ctx, "MarkTarget")

        mock_get_players.assert_not_called()
        self.ctx.reply.assert_not_called()
        await self.target_char.arefresh_from_db()
        self.assertIsNone(self.target_char.marked_wanted_until)

    async def test_cannot_mark_yourself(self):
        self.mock_players = [
            (
                "76561199000002001",
                {
                    "character_guid": "admin-guid-mark",
                    "name": "AdminCool",
                    "location": "X=0 Y=0 Z=0",
                },
            )
        ]
        await self._run(name="AdminCool")

        self.ctx.reply.assert_called_once()
        self.assertIn("cannot mark yourself", self.ctx.reply.call_args[0][0])
        await self.admin_char.arefresh_from_db()
        self.assertIsNone(self.admin_char.marked_wanted_until)

    async def test_unknown_player_replies_not_found(self):
        with patch(
            "amc.commands.police.get_players",
            new=AsyncMock(return_value=[]),
        ):
            await self.cmd_markwanted(self.ctx, "Ghost")

        self.ctx.reply.assert_called_once()
        self.assertIn("Player not found", self.ctx.reply.call_args[0][0])
