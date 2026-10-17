"""Tests for the ServerDespawnVehicle audit log handler."""

from django.test import TestCase

from amc.handlers.vehicle_despawn import handle_despawn_vehicle
from amc.models import Character, Player, ServerVehicleDespawnLog


class DespawnVehicleLogTestCase(TestCase):
    async def test_despawn_vehicle_event_is_logged(self):
        player = await Player.objects.acreate(unique_id=123, discord_user_id=456)
        character = await Character.objects.acreate(player=player, name="Tester")

        event = {
            "hook": "ServerDespawnVehicle",
            "timestamp": 1787912130,  # UTC epoch
            "data": {
                "CharacterGuid": "47588A084FBC9F53080CDE9184FC02A5",
                "Vehicle": {
                    "Name": "Golima_Semi_C",
                    "Net_VehicleId": 1104948,
                    "Class": "AMTVehicle",
                },
                "OwnerCharacterGuid": "0870EA8543467219000387AB1BADA8D0",
                "OwnerName": "SomeoneElse",
                "Cost": 0,
            },
        }

        result = await handle_despawn_vehicle(event, player, character, ctx=None)
        assert result == (0, 0, 0, 0)

        rows = [row async for row in ServerVehicleDespawnLog.objects.all()]
        assert len(rows) == 1
        row = rows[0]
        assert row.character_id == character.id
        assert row.player_id == player.unique_id
        assert row.hook == "ServerDespawnVehicle"
        assert row.vehicle_game_id == 1104948
        assert row.vehicle_name == "Golima_Semi_C"
        assert row.data["OwnerName"] == "SomeoneElse"
        assert row.data["OwnerCharacterGuid"] == "0870EA8543467219000387AB1BADA8D0"

    async def test_despawn_vehicle_event_without_character(self):
        # Dispatch may pass character=None for unresolvable callers.
        event = {
            "hook": "ServerDespawnVehicle",
            "timestamp": 1787912131,
            "data": {"Vehicle": {"Name": "Titan_C"}},
        }
        result = await handle_despawn_vehicle(event, None, None, ctx=None)
        assert result == (0, 0, 0, 0)

        rows = [row async for row in ServerVehicleDespawnLog.objects.all()]
        assert len(rows) == 1
        assert rows[0].character is None
        assert rows[0].player is None
        assert rows[0].vehicle_game_id is None
        assert rows[0].vehicle_name == "Titan_C"
