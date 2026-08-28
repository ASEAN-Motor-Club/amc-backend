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
            "bIsAdmin": False,
            "CustomDestinationAbsoluteLocation": {"X": 100, "Y": 200, "Z": 300},
        }

        self.player = Player.objects.create(unique_id="76561198000000000")
        self.character = Character.objects.create(
            name="TestChar", player=self.player, guid="guid-123"
        )
        self.requester = Character.objects.create(
            name="Requester",
            player=Player.objects.create(unique_id="76561198000000001"),
            guid="guid-456",
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
            character=self.requester, message="Help me", location=Point(100, 200, 300)
        )
        await rescue.responders.aadd(self.player)

        with (
            patch(
                "amc.commands.teleport.get_player_last_vehicle",
                new=AsyncMock(return_value={"vehicle": None}),
            ),
            patch("amc.commands.teleport.teleport_player", new=AsyncMock()) as mock_tp,
        ):
            # Should succeed now
            await cmd_tp_name(self.ctx, "")

            # Verify we tried to teleport
            mock_tp.assert_called_once()
            # And argument 2 (location) should match our custom dest
            call_args = mock_tp.call_args
            self.assertEqual(
                call_args[0][2], {"X": 100, "Y": 200, "Z": 305}
            )  # Z+5 logic


class TeleportAdminCustomDestinationTestCase(TestCase):
    def setUp(self):
        self.ctx = MagicMock(spec=CommandContext)
        self.ctx.reply = AsyncMock()
        self.ctx.announce = AsyncMock()

        self.ctx.http_client_mod = MagicMock()
        self.ctx.http_client_mod.post = AsyncMock()
        self.ctx.http_client_mod.get = AsyncMock()

        self.player = Player.objects.create(unique_id="76561198000000000")
        self.character = Character.objects.create(
            name="AdminChar", player=self.player, guid="guid-123"
        )

        self.ctx.character = self.character
        self.ctx.player = self.player

    async def test_cmd_tp_admin_fetches_custom_dest_from_mod_server(self):
        """
        When an admin types /tp with no args and the game-server player_info
        lacks CustomDestinationAbsoluteLocation, the command should fetch it
        from the mod server inline.
        """
        self.ctx.player_info = {
            "bIsAdmin": True,
            # Missing CustomDestinationAbsoluteLocation — simulates game server response
        }

        with (
            patch(
                "amc.commands.teleport.get_player_last_vehicle",
                new=AsyncMock(return_value={"vehicle": None}),
            ),
            patch(
                "amc.commands.teleport.get_player",
                new=AsyncMock(
                    return_value={
                        "CustomDestinationAbsoluteLocation": {"X": 500, "Y": 600, "Z": 700}
                    }
                ),
            ) as mock_get_player,
            patch("amc.commands.teleport.teleport_player", new=AsyncMock()) as mock_tp,
        ):
            await cmd_tp_name(self.ctx, "")

            mock_get_player.assert_called_once_with(
                self.ctx.http_client_mod, str(self.player.unique_id)
            )
            mock_tp.assert_called_once()
            call_args = mock_tp.call_args
            self.assertEqual(call_args[0][2], {"X": 500, "Y": 600, "Z": 705})  # Z+5


class TeleportWaypointHeightmapTestCase(TestCase):
    """Waypoint Z must be max(game Z, terrain Z).

    The heightmap is bare terrain — it does not include buildings, piers,
    etc. When the game-provided destination Z is already above the
    heightmap (destination sits on a structure), it must be kept.
    """

    def setUp(self):
        self.ctx = MagicMock(spec=CommandContext)
        self.ctx.reply = AsyncMock()
        self.ctx.announce = AsyncMock()

        self.ctx.http_client_mod = MagicMock()
        self.ctx.http_client_mod.post = AsyncMock()
        self.ctx.http_client_mod.get = AsyncMock()

        self.player = Player.objects.create(unique_id="76561198000000002")
        self.character = Character.objects.create(
            name="WaypointChar", player=self.player, guid="guid-789"
        )

        self.ctx.character = self.character
        self.ctx.player = self.player
        self.ctx.player_info = {
            "bIsAdmin": True,
            "CustomDestinationAbsoluteLocation": {"X": -500000, "Y": 400000, "Z": 300},
        }

    async def _run_tp(self, terrain_z):
        with (
            patch(
                "amc.commands.teleport.get_player_last_vehicle",
                new=AsyncMock(return_value={"vehicle": None}),
            ),
            patch(
                "amc.commands.teleport.terrain_z_cm",
                return_value=terrain_z,
            ) as mock_terrain,
            patch("amc.commands.teleport.teleport_player", new=AsyncMock()) as mock_tp,
        ):
            await cmd_tp_name(self.ctx, "")
            mock_terrain.assert_called_once_with(-500000, 400000)
            mock_tp.assert_called_once()
            return mock_tp.call_args[0][2]

    async def test_waypoint_z_raised_to_terrain_when_below(self):
        # Game Z below terrain (bogus waypoint Z) -> terrain wins.
        # VehicleKey missing -> else branch, Z + 5.
        location = await self._run_tp(terrain_z=-20000)
        self.assertEqual(location["Z"], -20000 + 5)

    async def test_waypoint_z_kept_when_already_above_terrain(self):
        # Game Z above terrain (clicked on a building/pier) -> keep game Z.
        location = await self._run_tp(terrain_z=-21900)
        self.assertEqual(location["Z"], 300 + 5)

    async def test_waypoint_z_on_foot_offset(self):
        # On-foot branch (VehicleKey == "None") uses +100; terrain still
        # lifts the bogus game Z.
        self.ctx.player_info = {
            "bIsAdmin": True,
            "VehicleKey": "None",
            "CustomDestinationAbsoluteLocation": {
                "X": -500000, "Y": 400000, "Z": 300
            },
        }
        location = await self._run_tp(terrain_z=-20000)
        self.assertEqual(location["Z"], -20000 + 100)
