"""_welcome_new_player must teleport pawn-agnostic (tp2marker-style).

Regression for 2026-08-29..09-06: the welcome flow force-exited the player's
vehicle then teleported with NoVehicles ~1 s later. The mod's exit endpoint
returns 200 fire-and-forget while the game applies ServerExitVehicle
asynchronously, so the pawn was still an MTVehicle when the teleport arrived
→ mod 400 "Player is inside a vehicle" → every new player (5/5 in the
retained prod logs) skipped the skydive teleport. The fix drops the exit
dance; the mod handler picks the path by pawn type (character →
ServerTeleportCharacter, vehicle → vehicle move with the player, same as
/tp2marker and the impound relocation).
"""

from unittest.mock import AsyncMock, MagicMock, patch

from django.contrib.gis.geos import Point
from django.test import TestCase

from amc.models import Character, Player, TeleportPoint
from amc.tasks import _welcome_new_player

SKYDIVE_XYZ = (-382312.0, 201790.0, 12345.0)


class WelcomeNewPlayerSkydiveTestCase(TestCase):
    PLAYER_ID = 76561198837904999
    GUID = "WELCOME-TEST-GUID-0001"
    NAME = "WelcomeTester"

    async def _make_player_character(self):
        player = await Player.objects.acreate(unique_id=self.PLAYER_ID)
        character = await Character.objects.acreate(
            name=self.NAME, player=player, guid=self.GUID
        )
        return player, character

    async def _cleanup(self, player):
        # TeleportPoint.character is SET_NULL: delete own TP rows explicitly
        # BEFORE the player (async acreate rows are not rolled back).
        await TeleportPoint.objects.filter(character__player=player).adelete()
        await Player.objects.filter(unique_id=player.unique_id).adelete()

    async def test_teleports_with_or_without_vehicle(self):
        player, character = await self._make_player_character()
        try:
            await TeleportPoint.objects.acreate(
                name="skydive",
                character=character,
                location=Point(*SKYDIVE_XYZ),
            )
            session = MagicMock()
            mock_tp = AsyncMock()
            mock_exit = AsyncMock()
            with (
                patch("amc.tasks.teleport_player", mock_tp),
                patch("amc.tasks.force_exit_vehicle", mock_exit),
            ):
                await _welcome_new_player(session, character, player)

            # The old flow force-exited first — must be gone entirely.
            mock_exit.assert_not_called()

            mock_tp.assert_awaited_once()
            args, kwargs = mock_tp.await_args
            self.assertEqual(args[0], session)
            self.assertEqual(args[1], str(player.unique_id))
            self.assertEqual(
                args[2],
                {"X": SKYDIVE_XYZ[0], "Y": SKYDIVE_XYZ[1], "Z": SKYDIVE_XYZ[2]},
            )
            # Pawn-agnostic: no NoVehicles flag (tp2marker behaviour).
            self.assertNotIn("no_vehicles", kwargs)
        finally:
            await self._cleanup(player)

    async def test_missing_skydive_point_skips_teleport(self):
        player, character = await self._make_player_character()
        try:
            mock_tp = AsyncMock()
            with patch("amc.tasks.teleport_player", mock_tp):
                # Must not raise — the whole flow is fire-and-forget.
                await _welcome_new_player(MagicMock(), character, player)
            mock_tp.assert_not_called()
        finally:
            await self._cleanup(player)

    async def test_teleport_failure_does_not_raise(self):
        player, character = await self._make_player_character()
        try:
            await TeleportPoint.objects.acreate(
                name="skydive",
                character=character,
                location=Point(*SKYDIVE_XYZ),
            )
            mock_tp = AsyncMock(side_effect=Exception("Player is inside a vehicle"))
            with patch("amc.tasks.teleport_player", mock_tp):
                # Fire-and-forget task must swallow teleport failures.
                await _welcome_new_player(MagicMock(), character, player)
            mock_tp.assert_awaited_once()
        finally:
            await self._cleanup(player)
