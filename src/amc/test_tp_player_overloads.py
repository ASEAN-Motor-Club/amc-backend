"""Tests for the /tp player overloads, /tpto, and deprecated /tp_player."""

from unittest.mock import AsyncMock, MagicMock, patch

from django.test import TestCase

from amc.command_framework import CommandContext, registry
from amc.commands.general import cmd_help
from amc.models import Character, Player

ALICE = ("123", {
    "name": "Alice",
    "character_guid": "guid-alice",
    "location": "X=100.0 Y=200.0 Z=300.0",
})
BOB = ("456", {
    "name": "Bob",
    "character_guid": "guid-bob",
    "location": "X=1000.0 Y=2000.0 Z=3000.0",
})


def make_ctx(character, player_info):
    ctx = MagicMock(spec=CommandContext)
    ctx.reply = AsyncMock()
    ctx.announce = AsyncMock()
    ctx.character = character
    ctx.player = character.player
    ctx.player_info = player_info
    ctx.http_client = MagicMock()
    ctx.http_client_mod = MagicMock()
    return ctx


def pt(name="dasa", x=1.0, y=2.0, z=3.0):
    point = MagicMock()
    point.name = name
    point.location.x, point.location.y, point.location.z = x, y, z
    return point


class TpOverloadTestCase(TestCase):
    async def _make_character(self, name="TpTester", guid="guid-tp-overload"):
        player = await Player.objects.acreate(unique_id="76561198000000077")
        return await Character.objects.acreate(name=name, player=player, guid=guid)

    async def test_tp_player_point_dispatches_to_helper(self):
        character = await self._make_character()
        ctx = make_ctx(character, {"bIsAdmin": True})
        with (
            patch(
                "amc.commands.admin.get_players", new=AsyncMock(return_value=[ALICE])
            ),
            patch(
                "amc.commands.admin.TeleportPoint.objects.aget",
                new=AsyncMock(return_value=pt()),
            ),
            patch(
                "amc.commands.admin.teleport_player", new=AsyncMock()
            ) as mock_tp,
            patch("amc.commands.admin.show_popup", new=AsyncMock()),
        ):
            handled = await registry.execute("/tp Alice dasa", ctx)
        self.assertTrue(handled)
        mock_tp.assert_awaited_once()
        self.assertEqual(mock_tp.await_args[0][1], "123")
        self.assertEqual(mock_tp.await_args[0][2], {"X": 1.0, "Y": 2.0, "Z": 3.0})

    async def test_tp_player_point_requires_admin(self):
        character = await self._make_character()
        ctx = make_ctx(character, {})
        with patch(
            "amc.commands.admin.teleport_player", new=AsyncMock()
        ) as mock_tp:
            handled = await registry.execute("/tp Alice dasa", ctx)
        self.assertTrue(handled)
        mock_tp.assert_not_called()

    async def test_tp_player_coords_dispatches_to_coords(self):
        character = await self._make_character()
        ctx = make_ctx(character, {"bIsAdmin": True})
        with (
            patch(
                "amc.commands.teleport.get_players",
                new=AsyncMock(return_value=[ALICE]),
            ),
            patch(
                "amc.commands.teleport.teleport_player", new=AsyncMock()
            ) as mock_tp,
            patch("amc.commands.teleport.show_popup", new=AsyncMock()),
        ):
            handled = await registry.execute("/tp Alice 500 -300 200", ctx)
        self.assertTrue(handled)
        mock_tp.assert_awaited_once()
        self.assertEqual(mock_tp.await_args[0][1], "123")
        self.assertEqual(
            mock_tp.await_args[0][2], {"X": 500, "Y": -300, "Z": 200}
        )

    async def test_tp_player_coords_requires_admin(self):
        character = await self._make_character()
        ctx = make_ctx(character, {})
        with patch(
            "amc.commands.teleport.teleport_player", new=AsyncMock()
        ) as mock_tp:
            handled = await registry.execute("/tp Alice 1 2 3", ctx)
        self.assertTrue(handled)
        mock_tp.assert_not_called()

    async def test_deprecated_tp_player_shows_deprecation(self):
        character = await self._make_character()
        ctx = make_ctx(character, {"bIsAdmin": True})
        with patch(
            "amc.commands.admin.teleport_player", new=AsyncMock()
        ) as mock_tp:
            handled = await registry.execute("/tp_player Alice dasa", ctx)
        self.assertTrue(handled)
        mock_tp.assert_not_called()
        self.assertIn("Deprecated", ctx.reply.await_args[0][0])


class TptoTestCase(TestCase):
    async def _make_character(self, name="TpToTester", guid="guid-tpto"):
        player = await Player.objects.acreate(unique_id="76561198000000088")
        return await Character.objects.acreate(name=name, player=player, guid=guid)

    async def test_tpto_self_teleports_caller_to_target(self):
        character = await self._make_character()
        ctx = make_ctx(character, {})
        with (
            patch(
                "amc.commands.teleport.get_players",
                new=AsyncMock(return_value=[BOB]),
            ),
            patch(
                "amc.commands.teleport.teleport_player", new=AsyncMock()
            ) as mock_tp,
        ):
            handled = await registry.execute("/tpto Bob", ctx)
        self.assertTrue(handled)
        mock_tp.assert_awaited_once()
        self.assertEqual(mock_tp.await_args[0][1], "76561198000000088")
        self.assertEqual(
            mock_tp.await_args[0][2], {"X": 1000.0, "Y": 2000.0, "Z": 3100.0}
        )
        self.assertTrue(mock_tp.await_args[2]["no_vehicles"])

    async def test_tpto_unknown_player_replies(self):
        character = await self._make_character()
        ctx = make_ctx(character, {})
        with (
            patch(
                "amc.commands.teleport.get_players", new=AsyncMock(return_value=[])
            ),
            patch(
                "amc.commands.teleport.teleport_player", new=AsyncMock()
            ) as mock_tp,
        ):
            handled = await registry.execute("/tpto Nobody", ctx)
        self.assertTrue(handled)
        mock_tp.assert_not_called()
        self.assertIn("not find", ctx.reply.await_args[0][0])

    async def test_tpto_two_players_requires_admin(self):
        character = await self._make_character()
        ctx = make_ctx(character, {})
        with patch(
            "amc.commands.teleport.teleport_player", new=AsyncMock()
        ) as mock_tp:
            handled = await registry.execute("/tpto Alice Bob", ctx)
        self.assertTrue(handled)
        mock_tp.assert_not_called()
        self.assertEqual(ctx.reply.await_args[0][0], "Admin Only")

    async def test_tpto_two_players_admin_teleports_first_to_second(self):
        character = await self._make_character()
        ctx = make_ctx(character, {"bIsAdmin": True})
        with (
            patch(
                "amc.commands.teleport.get_players",
                new=AsyncMock(return_value=[ALICE, BOB]),
            ),
            patch(
                "amc.commands.teleport.teleport_player", new=AsyncMock()
            ) as mock_tp,
            patch("amc.commands.teleport.show_popup", new=AsyncMock()),
        ):
            handled = await registry.execute("/tpto Alice Bob", ctx)
        self.assertTrue(handled)
        mock_tp.assert_awaited_once()
        self.assertEqual(mock_tp.await_args[0][1], "123")
        self.assertEqual(
            mock_tp.await_args[0][2], {"X": 1000.0, "Y": 2000.0, "Z": 3100.0}
        )


class HelpListTestCase(TestCase):
    async def test_help_hides_deprecated_tp_player(self):
        player = await Player.objects.acreate(unique_id="76561198000000099")
        character = await Character.objects.acreate(
            name="HelpTester", player=player, guid="guid-help"
        )
        ctx = make_ctx(character, {})
        await cmd_help(ctx)
        msg = ctx.reply.await_args[0][0]
        self.assertNotIn("tp_player", msg)
        self.assertIn("/tpto", msg)
