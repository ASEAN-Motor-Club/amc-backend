"""Tests for the guilds system (amc.guilds)."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone

from amc.factories import CharacterFactory, PlayerFactory
from amc.guilds import (
    _activate_guild,
    _end_active_session,
    _find_matching_guild_vehicle,
    check_guild_cargo,
    check_guild_passenger,
    handle_guild_session,
)
from amc.models import (
    CharacterLocation,
    DeliveryPoint,
    Guild,
    GuildCargoRequirement,
    GuildCharacter,
    GuildPassengerRequirement,
    GuildSession,
    GuildVehicle,
    GuildVehiclePart,
    ServerCargoArrivedLog,
    ServerPassengerArrivedLog,
    VehicleDecal,
)
from amc.webhook import process_event


class FindMatchingGuildVehicleTests(TestCase):
    """Unit tests for _find_matching_guild_vehicle."""

    async def _create_guild(self, **kwargs):
        defaults = {"name": "Test Guild", "abbreviation": "TST"}
        defaults.update(kwargs)
        return await Guild.objects.acreate(**defaults)

    async def _create_vehicle(self, guild, vehicle_key="Trophy2", decal=None, parts=None):
        gv = await GuildVehicle.objects.acreate(
            guild=guild, vehicle_key=vehicle_key, decal=decal
        )
        if parts:
            for pk in parts:
                await GuildVehiclePart.objects.acreate(guild_vehicle=gv, part_key=pk)
        return gv

    async def test_no_vehicles_returns_none(self):
        result = await _find_matching_guild_vehicle("Trophy2", "guid123", AsyncMock())
        self.assertIsNone(result)

    async def test_vehicle_key_match_no_parts(self):
        guild = await self._create_guild()
        gv = await self._create_vehicle(guild)
        result = await _find_matching_guild_vehicle("Trophy2", "guid123", AsyncMock())
        self.assertEqual(result, gv)

    async def test_unknown_vehicle_name_returns_none(self):
        guild = await self._create_guild()
        await self._create_vehicle(guild)
        result = await _find_matching_guild_vehicle("UnknownVehicle", "guid123", AsyncMock())
        self.assertIsNone(result)

    async def test_wrong_vehicle_returns_none(self):
        guild = await self._create_guild()
        await self._create_vehicle(guild, vehicle_key="Trophy2")
        result = await _find_matching_guild_vehicle("1", "guid123", AsyncMock())
        self.assertIsNone(result)

    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_part_match_success(self, mock_parts):
        guild = await self._create_guild()
        gv = await self._create_vehicle(guild, parts=["Bike_I4_90HP"])
        mock_parts.return_value = {
            "parts": [
                {"Key": "SomeBody", "Slot": 1},
                {"Key": "Bike_I4_90HP", "Slot": 2},
            ]
        }

        result = await _find_matching_guild_vehicle("Trophy2", "guid123", AsyncMock())
        self.assertEqual(result, gv)
        mock_parts.assert_awaited_once()

    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_part_mismatch_returns_none(self, mock_parts):
        guild = await self._create_guild()
        await self._create_vehicle(guild, parts=["Bike_I4_90HP"])
        mock_parts.return_value = {
            "parts": [
                {"Key": "DifferentEngine", "Slot": 2},
            ]
        }

        result = await _find_matching_guild_vehicle("Trophy2", "guid123", AsyncMock())
        self.assertIsNone(result)

    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_part_mismatch_falls_back_to_no_parts_vehicle(self, mock_parts):
        guild_no_parts = await self._create_guild(name="No Parts", abbreviation="NP")
        no_parts_gv = await self._create_vehicle(guild_no_parts, vehicle_key="Trophy2")
        guild_with_parts = await self._create_guild(name="Engine Guild", abbreviation="ENG")
        await self._create_vehicle(guild_with_parts, vehicle_key="Trophy2", parts=["Bike_I4_90HP"])
        mock_parts.return_value = {
            "parts": [
                {"Key": "DifferentEngine", "Slot": 2},
            ]
        }

        result = await _find_matching_guild_vehicle("Trophy2", "guid123", AsyncMock())
        self.assertEqual(result, no_parts_gv)

    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_no_matching_parts_falls_back(self, mock_parts):
        guild = await self._create_guild()
        no_parts_gv = await self._create_vehicle(guild)
        guild2 = await self._create_guild(name="Engine Guild", abbreviation="ENG")
        await self._create_vehicle(guild2, vehicle_key="Trophy2", parts=["Bike_I4_90HP"])
        mock_parts.return_value = {
            "parts": [
                {"Key": "SomeBody", "Slot": 1},
            ]
        }

        result = await _find_matching_guild_vehicle("Trophy2", "guid123", AsyncMock())
        self.assertEqual(result, no_parts_gv)

    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_api_failure_returns_none(self, mock_parts):
        guild = await self._create_guild()
        await self._create_vehicle(guild, parts=["Bike_I4_90HP"])
        mock_parts.side_effect = Exception("mod server down")

        result = await _find_matching_guild_vehicle("Trophy2", "guid123", AsyncMock())
        self.assertIsNone(result)

    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_no_api_call_when_no_parts_required(self, mock_parts):
        guild = await self._create_guild()
        await self._create_vehicle(guild)

        await _find_matching_guild_vehicle("Trophy2", "guid123", AsyncMock())
        mock_parts.assert_not_called()

    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_correct_vehicle_selected_among_multiple(self, mock_parts):
        guild_a = await self._create_guild(name="Guild A", abbreviation="GA")
        await self._create_vehicle(guild_a, vehicle_key="Trophy2", parts=["Engine_A"])
        guild_b = await self._create_guild(name="Guild B", abbreviation="GB")
        gv_b = await self._create_vehicle(guild_b, vehicle_key="Trophy2", parts=["Engine_B"])
        mock_parts.return_value = {
            "parts": [
                {"Key": "Engine_B", "Slot": 2},
            ]
        }

        result = await _find_matching_guild_vehicle("Trophy2", "guid123", AsyncMock())
        self.assertEqual(result, gv_b)

    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_multiple_parts_all_required(self, mock_parts):
        guild = await self._create_guild()
        gv = await self._create_vehicle(
            guild, parts=["TaxiLicenseItem", "Bike_I4_90HP"]
        )
        mock_parts.return_value = {
            "parts": [
                {"Key": "TaxiLicenseItem", "Slot": 100},
                {"Key": "Bike_I4_90HP", "Slot": 2},
                {"Key": "SomeBody", "Slot": 1},
            ]
        }

        result = await _find_matching_guild_vehicle("Trophy2", "guid123", AsyncMock())
        self.assertEqual(result, gv)

    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_multiple_parts_partial_match_fails(self, mock_parts):
        guild = await self._create_guild()
        await self._create_vehicle(guild, parts=["TaxiLicenseItem", "Bike_I4_90HP"])
        mock_parts.return_value = {
            "parts": [
                {"Key": "TaxiLicenseItem", "Slot": 100},
            ]
        }

        result = await _find_matching_guild_vehicle("Trophy2", "guid123", AsyncMock())
        self.assertIsNone(result)

    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_prefers_part_specific_match_over_fallback(self, mock_parts):
        guild_specific = await self._create_guild(name="Specific", abbreviation="SPC")
        gv_specific = await self._create_vehicle(
            guild_specific, vehicle_key="Trophy2", parts=["Bike_I4_90HP"]
        )
        guild_generic = await self._create_guild(name="Generic", abbreviation="GEN")
        await self._create_vehicle(guild_generic, vehicle_key="Trophy2")
        mock_parts.return_value = {
            "parts": [
                {"Key": "Bike_I4_90HP", "Slot": 2},
            ]
        }

        result = await _find_matching_guild_vehicle("Trophy2", "guid123", AsyncMock())
        self.assertEqual(result, gv_specific)


class EndActiveSessionTests(TestCase):
    """Tests for _end_active_session."""

    async def test_ends_active_session(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        session = await GuildSession.objects.acreate(
            guild=guild, character=character, started_at=timezone.now()
        )
        self.assertIsNone(session.ended_at)

        await _end_active_session(character)

        await session.arefresh_from_db()
        self.assertIsNotNone(session.ended_at)

    async def test_no_active_session_no_error(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await _end_active_session(character)

    async def test_only_ends_active_sessions(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        past = timezone.now() - timezone.timedelta(hours=1)
        ended_session = await GuildSession.objects.acreate(
            guild=guild, character=character, started_at=past, ended_at=past + timezone.timedelta(minutes=30)
        )
        active_session = await GuildSession.objects.acreate(
            guild=guild, character=character, started_at=timezone.now()
        )

        await _end_active_session(character)

        await ended_session.arefresh_from_db()
        await active_session.arefresh_from_db()
        self.assertIsNotNone(ended_session.ended_at)
        self.assertIsNotNone(active_session.ended_at)


class ActivateGuildTests(TestCase):
    """Tests for _activate_guild."""

    async def _create_vehicle(self, guild, vehicle_key="Trophy2", decal=None):
        return await GuildVehicle.objects.acreate(
            guild=guild, vehicle_key=vehicle_key, decal=decal
        )

    @patch("amc.guilds.refresh_player_name", new_callable=AsyncMock)
    async def test_creates_session_and_character(self, mock_refresh):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        gv = await self._create_vehicle(guild)
        mock_session = AsyncMock()

        await _activate_guild(character, gv, mock_session, str(player.unique_id))

        session = await GuildSession.objects.aget(character=character, guild=guild)
        self.assertIsNone(session.ended_at)

        gc = await GuildCharacter.objects.aget(character=character, guild=guild)
        self.assertEqual(gc.level, 1)

    @patch("amc.guilds.refresh_player_name", new_callable=AsyncMock)
    async def test_no_duplicate_when_already_active(self, mock_refresh):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        gv = await self._create_vehicle(guild)
        await GuildSession.objects.acreate(
            guild=guild, character=character, started_at=timezone.now()
        )
        mock_session = AsyncMock()

        await _activate_guild(character, gv, mock_session, str(player.unique_id))

        count = await GuildSession.objects.filter(
            character=character, guild=guild, ended_at__isnull=True
        ).acount()
        self.assertEqual(count, 1)

    @patch("amc.guilds.refresh_player_name", new_callable=AsyncMock)
    async def test_switches_guild(self, mock_refresh):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild_a = await Guild.objects.acreate(name="Guild A", abbreviation="GA")
        await self._create_vehicle(guild_a, vehicle_key="Trophy2")
        guild_b = await Guild.objects.acreate(name="Guild B", abbreviation="GB")
        gv_b = await self._create_vehicle(guild_b, vehicle_key="Hana")
        await GuildSession.objects.acreate(
            guild=guild_a, character=character, started_at=timezone.now()
        )
        mock_session = AsyncMock()

        await _activate_guild(character, gv_b, mock_session, str(player.unique_id))

        old_session = await GuildSession.objects.aget(character=character, guild=guild_a)
        self.assertIsNotNone(old_session.ended_at)

        new_session = await GuildSession.objects.aget(
            character=character, guild=guild_b, ended_at__isnull=True
        )
        self.assertIsNotNone(new_session)

    @patch("amc.guilds.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.guilds.get_decal", new_callable=AsyncMock)
    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    async def test_decal_applied(self, mock_set_decal, mock_get_decal, mock_refresh):
        mock_get_decal.return_value = {"decal": {"DecalLayers": []}}
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        decal_config = {"decal": {"DecalLayers": [{"DecalKey": "test"}]}}
        decal = await VehicleDecal.objects.acreate(
            name="Guild Decal",
            hash="testhash123",
            config=decal_config,
        )
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        gv = await self._create_vehicle(guild, decal=decal)
        mock_session = AsyncMock()

        await _activate_guild(character, gv, mock_session, str(player.unique_id))

        mock_get_decal.assert_awaited_once_with(mock_session, str(player.unique_id))
        mock_set_decal.assert_awaited_once_with(
            mock_session, str(player.unique_id), decal_config
        )

    @patch("amc.guilds.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.guilds.get_decal", new_callable=AsyncMock)
    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    async def test_decal_skipped_when_vehicle_has_decals(self, mock_set_decal, mock_get_decal, mock_refresh):
        mock_get_decal.return_value = {"decal": {"DecalLayers": [{"DecalKey": "existing"}]}}
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        decal_config = {"decal": {"DecalLayers": [{"DecalKey": "guild"}]}}
        decal = await VehicleDecal.objects.acreate(
            name="Guild Decal",
            hash="testhash789",
            config=decal_config,
        )
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        gv = await self._create_vehicle(guild, decal=decal)
        mock_session = AsyncMock()

        await _activate_guild(character, gv, mock_session, str(player.unique_id))

        mock_set_decal.assert_not_called()

    @patch("amc.guilds.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    async def test_no_decal_when_none(self, mock_set_decal, mock_refresh):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        gv = await self._create_vehicle(guild)
        mock_session = AsyncMock()

        await _activate_guild(character, gv, mock_session, str(player.unique_id))

        mock_set_decal.assert_not_called()

    @patch("amc.guilds.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.guilds.get_decal", new_callable=AsyncMock)
    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    async def test_decal_failure_does_not_crash(self, mock_set_decal, mock_get_decal, mock_refresh):
        mock_get_decal.return_value = {"decal": {"DecalLayers": []}}
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        decal = await VehicleDecal.objects.acreate(
            name="Guild Decal",
            hash="testhash456",
            config={"decal": {"DecalLayers": []}},
        )
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        gv = await self._create_vehicle(guild, decal=decal)
        mock_set_decal.side_effect = Exception("server error")
        mock_session = AsyncMock()

        await _activate_guild(character, gv, mock_session, str(player.unique_id))

        session = await GuildSession.objects.aget(character=character, guild=guild)
        self.assertIsNone(session.ended_at)

    @patch("amc.guilds.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.guilds.get_decal", new_callable=AsyncMock)
    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    async def test_get_decal_failure_does_not_crash(self, mock_set_decal, mock_get_decal, mock_refresh):
        mock_get_decal.side_effect = Exception("connection error")
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        decal = await VehicleDecal.objects.acreate(
            name="Guild Decal",
            hash="testhash999",
            config={"decal": {"DecalLayers": [{"DecalKey": "test"}]}},
        )
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        gv = await self._create_vehicle(guild, decal=decal)
        mock_session = AsyncMock()

        await _activate_guild(character, gv, mock_session, str(player.unique_id))

        session = await GuildSession.objects.aget(character=character, guild=guild)
        self.assertIsNone(session.ended_at)
        mock_set_decal.assert_not_called()

    @patch("amc.guilds.refresh_player_name", new_callable=AsyncMock)
    async def test_guild_character_not_duplicated(self, mock_refresh):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        gv = await self._create_vehicle(guild)
        await GuildCharacter.objects.acreate(guild=guild, character=character, level=3)
        mock_session = AsyncMock()

        await _activate_guild(character, gv, mock_session, str(player.unique_id))

        count = await GuildCharacter.objects.filter(
            guild=guild, character=character
        ).acount()
        self.assertEqual(count, 1)
        gc = await GuildCharacter.objects.aget(guild=guild, character=character)
        self.assertEqual(gc.level, 3)


class HandleGuildSessionTests(TestCase):
    """Integration tests for handle_guild_session."""

    @patch("amc.guilds.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_entered_matching_vehicle(self, mock_parts, mock_decal, mock_refresh):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(name="Trophy Club", abbreviation="TC")
        await GuildVehicle.objects.acreate(guild=guild, vehicle_key="Trophy2")

        await handle_guild_session(
            character, player, AsyncMock(), "ENTERED", "Trophy2"
        )

        session = await GuildSession.objects.aget(character=character, guild=guild)
        self.assertIsNone(session.ended_at)
        gc = await GuildCharacter.objects.aget(character=character, guild=guild)
        self.assertEqual(gc.level, 1)
        mock_parts.assert_not_called()

    @patch("amc.guilds.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_entered_non_matching_vehicle(self, mock_parts, mock_decal, mock_refresh):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)

        await handle_guild_session(
            character, player, AsyncMock(), "ENTERED", "1"
        )

        count = await GuildSession.objects.filter(character=character).acount()
        self.assertEqual(count, 0)

    @patch("amc.guilds.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_exited_ends_session(self, mock_parts, mock_decal, mock_refresh):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        await GuildVehicle.objects.acreate(guild=guild, vehicle_key="Trophy2")
        await GuildSession.objects.acreate(
            guild=guild, character=character, started_at=timezone.now()
        )

        await handle_guild_session(
            character, player, AsyncMock(), "EXITED", "Trophy2"
        )

        session = await GuildSession.objects.aget(character=character, guild=guild)
        self.assertIsNotNone(session.ended_at)

    @patch("amc.guilds.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_entered_different_vehicle_ends_session(self, mock_parts, mock_decal, mock_refresh):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        await GuildVehicle.objects.acreate(guild=guild, vehicle_key="Trophy2")
        await GuildSession.objects.acreate(
            guild=guild, character=character, started_at=timezone.now()
        )

        await handle_guild_session(
            character, player, AsyncMock(), "ENTERED", "1"
        )

        session = await GuildSession.objects.aget(character=character, guild=guild)
        self.assertIsNotNone(session.ended_at)

    @patch("amc.guilds.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.guilds.get_decal", new_callable=AsyncMock)
    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_entered_with_part_match(self, mock_parts, mock_set_decal, mock_get_decal, mock_refresh):
        mock_get_decal.return_value = {"decal": {"DecalLayers": []}}
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        decal = await VehicleDecal.objects.acreate(
            name="Test Decal", hash="hash789", config={"decal": {"DecalLayers": []}}
        )
        guild = await Guild.objects.acreate(name="Engine Guild", abbreviation="ENG")
        gv = await GuildVehicle.objects.acreate(
            guild=guild, vehicle_key="Trophy2", decal=decal
        )
        await GuildVehiclePart.objects.acreate(guild_vehicle=gv, part_key="Bike_I4_90HP")
        mock_parts.return_value = {
            "parts": [
                {"Key": "Bike_I4_90HP", "Slot": 2},
            ]
        }
        mock_http = AsyncMock()

        await handle_guild_session(
            character, player, mock_http, "ENTERED", "Trophy2"
        )

        session = await GuildSession.objects.aget(character=character, guild=guild)
        self.assertIsNone(session.ended_at)
        mock_set_decal.assert_awaited_once()

    @patch("amc.guilds.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_entered_with_part_mismatch(self, mock_parts, mock_decal, mock_refresh):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(name="Engine Guild", abbreviation="ENG")
        gv = await GuildVehicle.objects.acreate(guild=guild, vehicle_key="Trophy2")
        await GuildVehiclePart.objects.acreate(guild_vehicle=gv, part_key="Bike_I4_90HP")
        mock_parts.return_value = {
            "parts": [
                {"Key": "DifferentEngine", "Slot": 2},
            ]
        }

        await handle_guild_session(
            character, player, AsyncMock(), "ENTERED", "Trophy2"
        )

        count = await GuildSession.objects.filter(character=character).acount()
        self.assertEqual(count, 0)

    @patch("amc.guilds.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_switch_guild_on_vehicle_change(self, mock_parts, mock_decal, mock_refresh):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild_a = await Guild.objects.acreate(name="Trophy Club", abbreviation="TC")
        await GuildVehicle.objects.acreate(guild=guild_a, vehicle_key="Trophy2")
        guild_b = await Guild.objects.acreate(name="Hana Club", abbreviation="HC")
        await GuildVehicle.objects.acreate(guild=guild_b, vehicle_key="1")

        await handle_guild_session(
            character, player, AsyncMock(), "ENTERED", "Trophy2"
        )
        session_a = await GuildSession.objects.aget(character=character, guild=guild_a)
        self.assertIsNone(session_a.ended_at)

        await handle_guild_session(
            character, player, AsyncMock(), "ENTERED", "1"
        )
        await session_a.arefresh_from_db()
        self.assertIsNotNone(session_a.ended_at)

        session_b = await GuildSession.objects.aget(
            character=character, guild=guild_b, ended_at__isnull=True
        )
        self.assertIsNotNone(session_b)

    @patch("amc.guilds.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_exception_does_not_propagate(self, mock_parts, mock_decal, mock_refresh):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        await GuildVehicle.objects.acreate(guild=guild, vehicle_key="Trophy2")
        mock_decal.side_effect = Exception("unexpected")

        await handle_guild_session(
            character, player, AsyncMock(), "ENTERED", "Trophy2"
        )

    @patch("amc.guilds.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.guilds.get_decal", new_callable=AsyncMock)
    @patch("amc.guilds.set_decal", new_callable=AsyncMock)
    @patch("amc.guilds.get_player_last_vehicle_parts", new_callable=AsyncMock)
    async def test_per_vehicle_decal_selection(self, mock_parts, mock_set_decal, mock_get_decal, mock_refresh):
        mock_get_decal.return_value = {"decal": {"DecalLayers": []}}
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        decal_a_config = {"decal": {"DecalLayers": [{"DecalKey": "a"}]}}
        decal_b_config = {"decal": {"DecalLayers": [{"DecalKey": "b"}]}}
        decal_a = await VehicleDecal.objects.acreate(
            name="Decal A", hash="hash_a", config=decal_a_config
        )
        decal_b = await VehicleDecal.objects.acreate(
            name="Decal B", hash="hash_b", config=decal_b_config
        )
        guild = await Guild.objects.acreate(name="Multi", abbreviation="MLT")
        await GuildVehicle.objects.acreate(
            guild=guild, vehicle_key="Trophy2", decal=decal_a
        )
        await GuildVehicle.objects.acreate(
            guild=guild, vehicle_key="1", decal=decal_b
        )
        mock_http = AsyncMock()

        await handle_guild_session(
            character, player, mock_http, "ENTERED", "Trophy2"
        )
        mock_set_decal.assert_awaited_once_with(
            mock_http, str(player.unique_id), decal_a_config
        )


class CoalesceTests(TestCase):
    """Tests for the _coalesce singleflight helper in mod_server."""

    async def test_concurrent_calls_share_same_request(self):
        from amc.mod_server import _coalesce

        store: dict = {}
        call_count = 0

        async def slow_fetch():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return {"result": "ok"}

        results = await asyncio.gather(
            _coalesce("key1", store, slow_fetch),
            _coalesce("key1", store, slow_fetch),
            _coalesce("key1", store, slow_fetch),
        )

        self.assertEqual(call_count, 1)
        self.assertEqual(results[0], {"result": "ok"})
        self.assertEqual(results[1], {"result": "ok"})
        self.assertEqual(results[2], {"result": "ok"})
        self.assertEqual(len(store), 0)

    async def test_different_keys_are_independent(self):
        from amc.mod_server import _coalesce

        store: dict = {}
        call_count = 0

        async def fetch():
            nonlocal call_count
            call_count += 1
            return call_count

        r1, r2 = await asyncio.gather(
            _coalesce("a", store, fetch),
            _coalesce("b", store, fetch),
        )

        self.assertEqual(call_count, 2)
        self.assertNotEqual(r1, r2)

    async def test_error_propagates_to_all_waiters(self):
        from amc.mod_server import _coalesce

        store: dict = {}

        async def failing_fetch():
            raise ValueError("boom")

        async def run():
            return await _coalesce("key1", store, failing_fetch)

        with self.assertRaises(ValueError):
            await asyncio.gather(run(), run())

        self.assertEqual(len(store), 0)

    async def test_cleanup_after_success(self):
        from amc.mod_server import _coalesce

        store: dict = {}

        async def fetch():
            return "done"

        await _coalesce("key1", store, fetch)
        self.assertNotIn("key1", store)

    async def test_cleanup_after_error(self):
        from amc.mod_server import _coalesce

        store: dict = {}

        async def failing_fetch():
            raise RuntimeError("fail")

        try:
            await _coalesce("key1", store, failing_fetch)
        except RuntimeError:
            pass

        self.assertNotIn("key1", store)


# ---------------------------------------------------------------------------
# check_guild_cargo tests
# ---------------------------------------------------------------------------


class CheckGuildCargoTests(TestCase):
    """Tests for check_guild_cargo."""

    async def _setup(self, cargo_req_kwargs=None):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        session = await GuildSession.objects.acreate(
            guild=guild, character=character, started_at=timezone.now()
        )
        if cargo_req_kwargs is not None:
            await GuildCargoRequirement.objects.acreate(
                guild=guild, **cargo_req_kwargs
            )
        return character, session

    async def test_no_session_returns_none(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        session, bonus = await check_guild_cargo(character, "SmallBox", 5000, 0)
        self.assertIsNone(session)
        self.assertEqual(bonus, 0)

    async def test_no_requirement_returns_none(self):
        character, _ = await self._setup()
        session, bonus = await check_guild_cargo(character, "SmallBox", 5000, 0)
        self.assertIsNone(session)
        self.assertEqual(bonus, 0)

    async def test_allowed_cargo_key_passes(self):
        character, sess = await self._setup(
            {"allowed_cargo_keys": ["SmallBox", "AppleBox"], "bonus_pct": 10}
        )
        session, bonus = await check_guild_cargo(character, "SmallBox", 5000, 0)
        self.assertEqual(session.pk, sess.pk)
        self.assertEqual(bonus, 500)

    async def test_allowed_cargo_key_fails(self):
        character, _ = await self._setup(
            {"allowed_cargo_keys": ["SmallBox"], "bonus_pct": 10}
        )
        session, bonus = await check_guild_cargo(character, "AppleBox", 5000, 0)
        self.assertIsNone(session)
        self.assertEqual(bonus, 0)

    async def test_empty_allowed_list_accepts_any(self):
        character, sess = await self._setup({"bonus_pct": 5})
        session, bonus = await check_guild_cargo(character, "Anything", 10000, 0)
        self.assertEqual(session.pk, sess.pk)
        self.assertEqual(bonus, 500)

    async def test_excluded_cargo_key_blocks(self):
        character, _ = await self._setup(
            {"excluded_cargo_keys": ["Ganja"], "bonus_pct": 10}
        )
        session, bonus = await check_guild_cargo(character, "Ganja", 5000, 0)
        self.assertIsNone(session)
        self.assertEqual(bonus, 0)

    async def test_excluded_cargo_key_allows_others(self):
        character, sess = await self._setup(
            {"excluded_cargo_keys": ["Ganja"], "bonus_pct": 10}
        )
        session, bonus = await check_guild_cargo(character, "SmallBox", 5000, 0)
        self.assertEqual(session.pk, sess.pk)
        self.assertEqual(bonus, 500)

    async def test_max_damage_filter_passes(self):
        character, sess = await self._setup({"max_damage": 0.5, "bonus_pct": 10})
        session, bonus = await check_guild_cargo(character, "SmallBox", 5000, 0.3)
        self.assertEqual(session.pk, sess.pk)

    async def test_max_damage_filter_fails(self):
        character, _ = await self._setup({"max_damage": 0.5, "bonus_pct": 10})
        session, bonus = await check_guild_cargo(character, "SmallBox", 5000, 0.8)
        self.assertIsNone(session)
        self.assertEqual(bonus, 0)

    async def test_null_max_damage_accepts_any(self):
        character, sess = await self._setup({"max_damage": None, "bonus_pct": 10})
        session, bonus = await check_guild_cargo(character, "SmallBox", 5000, 1.0)
        self.assertEqual(session.pk, sess.pk)

    async def test_min_payment_filter_passes(self):
        character, sess = await self._setup({"min_payment": 1000, "bonus_pct": 10})
        session, bonus = await check_guild_cargo(character, "SmallBox", 5000, 0)
        self.assertEqual(session.pk, sess.pk)

    async def test_min_payment_filter_fails(self):
        character, _ = await self._setup({"min_payment": 10000, "bonus_pct": 10})
        session, bonus = await check_guild_cargo(character, "SmallBox", 5000, 0)
        self.assertIsNone(session)

    async def test_max_payment_filter_passes(self):
        character, sess = await self._setup({"max_payment": 10000, "bonus_pct": 10})
        session, bonus = await check_guild_cargo(character, "SmallBox", 5000, 0)
        self.assertEqual(session.pk, sess.pk)

    async def test_max_payment_filter_fails(self):
        character, _ = await self._setup({"max_payment": 1000, "bonus_pct": 10})
        session, bonus = await check_guild_cargo(character, "SmallBox", 5000, 0)
        self.assertIsNone(session)

    async def test_bonus_calculation(self):
        character, sess = await self._setup({"bonus_pct": 15})
        session, bonus = await check_guild_cargo(character, "SmallBox", 10000, 0)
        self.assertEqual(bonus, 1500)

    async def test_zero_bonus_pct(self):
        character, sess = await self._setup({"bonus_pct": 0})
        session, bonus = await check_guild_cargo(character, "SmallBox", 10000, 0)
        self.assertEqual(session.pk, sess.pk)
        self.assertEqual(bonus, 0)

    async def test_ended_session_ignored(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        await GuildCargoRequirement.objects.acreate(
            guild=guild, bonus_pct=10
        )
        await GuildSession.objects.acreate(
            guild=guild, character=character,
            started_at=timezone.now(), ended_at=timezone.now(),
        )
        session, bonus = await check_guild_cargo(character, "SmallBox", 5000, 0)
        self.assertIsNone(session)
        self.assertEqual(bonus, 0)

    async def test_combined_filters(self):
        character, sess = await self._setup(
            {
                "allowed_cargo_keys": ["SmallBox", "Fuel"],
                "excluded_cargo_keys": ["Fuel"],
                "max_damage": 0.3,
                "min_payment": 1000,
                "max_payment": 20000,
                "bonus_pct": 20,
            }
        )
        session, bonus = await check_guild_cargo(character, "SmallBox", 5000, 0.1)
        self.assertEqual(session.pk, sess.pk)
        self.assertEqual(bonus, 1000)

    async def test_combined_filters_one_fails(self):
        character, _ = await self._setup(
            {
                "allowed_cargo_keys": ["SmallBox", "Fuel"],
                "max_damage": 0.3,
                "min_payment": 1000,
                "max_payment": 20000,
                "bonus_pct": 20,
            }
        )
        session, bonus = await check_guild_cargo(character, "AppleBox", 5000, 0.1)
        self.assertIsNone(session)


# ---------------------------------------------------------------------------
# check_guild_passenger tests
# ---------------------------------------------------------------------------


class CheckGuildPassengerTests(TestCase):
    """Tests for check_guild_passenger."""

    async def _setup(self, passenger_req_kwargs=None):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        session = await GuildSession.objects.acreate(
            guild=guild, character=character, started_at=timezone.now()
        )
        if passenger_req_kwargs is not None:
            await GuildPassengerRequirement.objects.acreate(
                guild=guild, **passenger_req_kwargs
            )
        return character, session

    async def test_no_session_returns_none(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        session, bonus = await check_guild_passenger(
            character, 2, True, False, False, False, 3, 5000
        )
        self.assertIsNone(session)
        self.assertEqual(bonus, 0)

    async def test_no_requirement_returns_none(self):
        character, _ = await self._setup()
        session, bonus = await check_guild_passenger(
            character, 2, True, False, False, False, 3, 5000
        )
        self.assertIsNone(session)
        self.assertEqual(bonus, 0)

    async def test_allowed_passenger_type_passes(self):
        character, sess = await self._setup(
            {"allowed_passenger_types": [2, 3], "bonus_pct": 10}
        )
        session, bonus = await check_guild_passenger(
            character, 2, False, False, False, False, None, 5000
        )
        self.assertEqual(session.pk, sess.pk)
        self.assertEqual(bonus, 500)

    async def test_allowed_passenger_type_fails(self):
        character, _ = await self._setup(
            {"allowed_passenger_types": [2], "bonus_pct": 10}
        )
        session, bonus = await check_guild_passenger(
            character, 1, False, False, False, False, None, 5000
        )
        self.assertIsNone(session)

    async def test_empty_allowed_list_accepts_any(self):
        character, sess = await self._setup({"bonus_pct": 5})
        session, bonus = await check_guild_passenger(
            character, 4, False, False, False, False, None, 10000
        )
        self.assertEqual(session.pk, sess.pk)
        self.assertEqual(bonus, 500)

    async def test_require_comfort_true_passes(self):
        character, sess = await self._setup(
            {"require_comfort": True, "bonus_pct": 10}
        )
        session, bonus = await check_guild_passenger(
            character, 2, True, False, False, False, 3, 5000
        )
        self.assertEqual(session.pk, sess.pk)

    async def test_require_comfort_true_fails(self):
        character, _ = await self._setup(
            {"require_comfort": True, "bonus_pct": 10}
        )
        session, bonus = await check_guild_passenger(
            character, 2, False, False, False, False, 3, 5000
        )
        self.assertIsNone(session)

    async def test_require_comfort_false_passes(self):
        character, sess = await self._setup(
            {"require_comfort": False, "bonus_pct": 10}
        )
        session, bonus = await check_guild_passenger(
            character, 2, False, False, False, False, 3, 5000
        )
        self.assertEqual(session.pk, sess.pk)

    async def test_require_comfort_null_accepts_any(self):
        character, sess = await self._setup(
            {"require_comfort": None, "bonus_pct": 10}
        )
        session, bonus = await check_guild_passenger(
            character, 2, True, False, False, False, 3, 5000
        )
        self.assertEqual(session.pk, sess.pk)
        session2, _ = await check_guild_passenger(
            character, 2, False, False, False, False, 3, 5000
        )
        self.assertEqual(session2.pk, sess.pk)

    async def test_require_urgent(self):
        character, _ = await self._setup(
            {"require_urgent": True, "bonus_pct": 10}
        )
        session, bonus = await check_guild_passenger(
            character, 2, False, False, False, False, 3, 5000
        )
        self.assertIsNone(session)

    async def test_require_limo(self):
        character, sess = await self._setup(
            {"require_limo": True, "bonus_pct": 10}
        )
        session, bonus = await check_guild_passenger(
            character, 2, True, False, True, False, 5, 5000
        )
        self.assertEqual(session.pk, sess.pk)

    async def test_require_offroad(self):
        character, _ = await self._setup(
            {"require_offroad": True, "bonus_pct": 10}
        )
        session, bonus = await check_guild_passenger(
            character, 2, False, False, False, False, 3, 5000
        )
        self.assertIsNone(session)

    async def test_min_comfort_rating_passes(self):
        character, sess = await self._setup(
            {"min_comfort_rating": 3, "bonus_pct": 10}
        )
        session, bonus = await check_guild_passenger(
            character, 2, True, False, False, False, 4, 5000
        )
        self.assertEqual(session.pk, sess.pk)

    async def test_min_comfort_rating_fails(self):
        character, _ = await self._setup(
            {"min_comfort_rating": 3, "bonus_pct": 10}
        )
        session, bonus = await check_guild_passenger(
            character, 2, True, False, False, False, 2, 5000
        )
        self.assertIsNone(session)

    async def test_max_comfort_rating_passes(self):
        character, sess = await self._setup(
            {"max_comfort_rating": 5, "bonus_pct": 10}
        )
        session, bonus = await check_guild_passenger(
            character, 2, True, False, False, False, 3, 5000
        )
        self.assertEqual(session.pk, sess.pk)

    async def test_max_comfort_rating_fails(self):
        character, _ = await self._setup(
            {"max_comfort_rating": 3, "bonus_pct": 10}
        )
        session, bonus = await check_guild_passenger(
            character, 2, True, False, False, False, 5, 5000
        )
        self.assertIsNone(session)

    async def test_comfort_rating_ignored_when_comfort_false(self):
        character, sess = await self._setup(
            {"min_comfort_rating": 3, "bonus_pct": 10}
        )
        session, bonus = await check_guild_passenger(
            character, 2, False, False, False, False, 0, 5000
        )
        self.assertEqual(session.pk, sess.pk)

    async def test_max_comfort_rating_zero_allows_comfort_false(self):
        character, sess = await self._setup(
            {"max_comfort_rating": 0, "bonus_pct": 10}
        )
        session, bonus = await check_guild_passenger(
            character, 2, False, False, False, False, 3, 5000
        )
        self.assertEqual(session.pk, sess.pk)

    async def test_max_comfort_rating_zero_blocks_comfort_true(self):
        character, _ = await self._setup(
            {"max_comfort_rating": 0, "bonus_pct": 10}
        )
        session, bonus = await check_guild_passenger(
            character, 2, True, False, False, False, 3, 5000
        )
        self.assertIsNone(session)

    async def test_bonus_calculation(self):
        character, sess = await self._setup({"bonus_pct": 25})
        session, bonus = await check_guild_passenger(
            character, 2, False, False, False, False, None, 8000
        )
        self.assertEqual(session.pk, sess.pk)
        self.assertEqual(bonus, 2000)

    async def test_zero_bonus_pct(self):
        character, sess = await self._setup({"bonus_pct": 0})
        session, bonus = await check_guild_passenger(
            character, 2, False, False, False, False, None, 8000
        )
        self.assertEqual(session.pk, sess.pk)
        self.assertEqual(bonus, 0)

    async def test_combined_flags(self):
        character, sess = await self._setup(
            {
                "allowed_passenger_types": [2],
                "require_comfort": True,
                "require_limo": True,
                "min_comfort_rating": 3,
                "bonus_pct": 15,
            }
        )
        session, bonus = await check_guild_passenger(
            character, 2, True, False, True, False, 4, 10000
        )
        self.assertEqual(session.pk, sess.pk)
        self.assertEqual(bonus, 1500)

    async def test_combined_flags_one_fails(self):
        character, _ = await self._setup(
            {
                "allowed_passenger_types": [2],
                "require_comfort": True,
                "require_limo": True,
                "min_comfort_rating": 3,
                "bonus_pct": 15,
            }
        )
        session, bonus = await check_guild_passenger(
            character, 2, True, False, False, False, 4, 10000
        )
        self.assertIsNone(session)


# ---------------------------------------------------------------------------
# Handler integration tests — cargo guild bonus
# ---------------------------------------------------------------------------


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
@patch("amc_finance.services.send_fund_to_player_wallet", new_callable=AsyncMock)
@patch("amc_finance.services.check_treasury_floor", new_callable=AsyncMock)
@patch("amc.handlers.cargo.transfer_money", new_callable=AsyncMock)
class CargoGuildBonusIntegrationTests(TestCase):
    """Integration tests for guild bonus applied in cargo handler."""

    async def _setup(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )
        await DeliveryPoint.objects.acreate(guid="gs", name="Mine", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="gd", name="Factory", coord=Point(100_000, 0, 0)
        )
        return player, character

    async def _activate_guild_session(self, character, cargo_req_kwargs=None):
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        if cargo_req_kwargs is not None:
            await GuildCargoRequirement.objects.acreate(
                guild=guild, **cargo_req_kwargs
            )
        await GuildSession.objects.acreate(
            guild=guild, character=character, started_at=timezone.now()
        )
        return guild

    def _cargo_event(self, character, player, cargo_key="SmallBox", payment=5000, damage=0.0):
        return {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "Cargos": [
                    {
                        "Net_CargoKey": cargo_key,
                        "Net_Payment": payment,
                        "Net_Weight": 100.0,
                        "Net_Damage": damage,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100_000, "Y": 0, "Z": 0},
                    }
                ],
                "PlayerId": str(player.unique_id),
                "CharacterGuid": str(character.guid),
            },
        }

    async def test_bonus_applied_to_payment(self, mock_transfer, mock_floor, mock_wallet, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        mock_floor.return_value = True
        player, character = await self._setup()
        await self._activate_guild_session(character, {"bonus_pct": 20})

        base_pay, _, _, _ = await process_event(
            self._cargo_event(character, player, payment=10_000),
            player,
            character,
            http_client_mod=AsyncMock(),
        )

        log = await ServerCargoArrivedLog.objects.afirst()
        self.assertEqual(log.payment, 12_000)
        self.assertEqual(base_pay, 12_000)
        self.assertIsNotNone(log.guild_session_id)

    async def test_bonus_not_applied_when_no_requirement(self, mock_transfer, mock_floor, mock_wallet, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        mock_floor.return_value = True
        player, character = await self._setup()
        await self._activate_guild_session(character)

        base_pay, _, _, _ = await process_event(
            self._cargo_event(character, player, payment=10_000),
            player,
            character,
            http_client_mod=AsyncMock(),
        )

        log = await ServerCargoArrivedLog.objects.afirst()
        self.assertEqual(log.payment, 10_000)
        self.assertIsNone(log.guild_session_id)

    async def test_bonus_not_applied_when_filter_fails(self, mock_transfer, mock_floor, mock_wallet, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        mock_floor.return_value = True
        player, character = await self._setup()
        await self._activate_guild_session(
            character, {"allowed_cargo_keys": ["Fuel"], "bonus_pct": 20}
        )

        base_pay, _, _, _ = await process_event(
            self._cargo_event(character, player, cargo_key="SmallBox", payment=10_000),
            player,
            character,
            http_client_mod=AsyncMock(),
        )

        log = await ServerCargoArrivedLog.objects.afirst()
        self.assertEqual(log.payment, 10_000)
        self.assertIsNone(log.guild_session_id)

    async def test_bonus_on_multiple_cargos(self, mock_transfer, mock_floor, mock_wallet, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        mock_floor.return_value = True
        player, character = await self._setup()
        await self._activate_guild_session(character, {"bonus_pct": 10})

        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "Cargos": [
                    {
                        "Net_CargoKey": "SmallBox",
                        "Net_Payment": 5000,
                        "Net_Weight": 100.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100_000, "Y": 0, "Z": 0},
                    },
                    {
                        "Net_CargoKey": "SmallBox",
                        "Net_Payment": 5000,
                        "Net_Weight": 100.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100_000, "Y": 0, "Z": 0},
                    },
                ],
                "PlayerId": str(player.unique_id),
                "CharacterGuid": str(character.guid),
            },
        }

        base_pay, _, _, _ = await process_event(event, player, character, http_client_mod=AsyncMock())

        logs = [log async for log in ServerCargoArrivedLog.objects.all()]
        self.assertEqual(len(logs), 2)
        for log in logs:
            self.assertEqual(log.payment, 5500)
            self.assertIsNotNone(log.guild_session_id)
        self.assertEqual(base_pay, 11_000)

    async def test_damage_filter_blocks_bonus(self, mock_transfer, mock_floor, mock_wallet, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        mock_floor.return_value = True
        player, character = await self._setup()
        await self._activate_guild_session(
            character, {"max_damage": 0.3, "bonus_pct": 20}
        )

        base_pay, _, _, _ = await process_event(
            self._cargo_event(character, player, payment=10_000, damage=0.5),
            player,
            character,
            http_client_mod=AsyncMock(),
        )

        log = await ServerCargoArrivedLog.objects.afirst()
        self.assertEqual(log.payment, 10_000)
        self.assertIsNone(log.guild_session_id)


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
@patch("amc_finance.services.send_fund_to_player_wallet", new_callable=AsyncMock)
@patch("amc_finance.services.check_treasury_floor", new_callable=AsyncMock)
@patch("amc.handlers.passenger.transfer_money", new_callable=AsyncMock)
class PassengerGuildBonusIntegrationTests(TestCase):
    """Integration tests for guild bonus applied in passenger handler."""

    async def _setup(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        return player, character

    async def _activate_guild_session(self, character, passenger_req_kwargs=None):
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        if passenger_req_kwargs is not None:
            await GuildPassengerRequirement.objects.acreate(
                guild=guild, **passenger_req_kwargs
            )
        await GuildSession.objects.acreate(
            guild=guild, character=character, started_at=timezone.now()
        )
        return guild

    def _passenger_event(self, character, player, passenger_type=2, payment=3000):
        return {
            "hook": "ServerPassengerArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "PlayerId": str(player.unique_id),
                "Passenger": {
                    "Net_PassengerType": passenger_type,
                    "Net_Payment": payment,
                    "Net_Distance": 500.0,
                    "Net_bArrived": True,
                    "Net_PassengerFlags": 0,
                    "Net_LCComfortSatisfaction": 0,
                    "Net_StartLocation": {"X": 10, "Y": 10, "Z": 0},
                },
            },
        }

    async def test_bonus_applied_to_passenger_payment(self, mock_transfer, mock_floor, mock_wallet, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        mock_floor.return_value = True
        player, character = await self._setup()
        await self._activate_guild_session(character, {"bonus_pct": 15})

        base_pay, _, _, _ = await process_event(
            self._passenger_event(character, player, payment=4000),
            player,
            character,
            http_client_mod=AsyncMock(),
        )

        log = await ServerPassengerArrivedLog.objects.afirst()
        self.assertEqual(log.payment, 4600)
        self.assertEqual(base_pay, 4600)
        self.assertIsNotNone(log.guild_session_id)

    async def test_bonus_not_applied_when_no_requirement(self, mock_transfer, mock_floor, mock_wallet, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        mock_floor.return_value = True
        player, character = await self._setup()
        await self._activate_guild_session(character)

        base_pay, _, _, _ = await process_event(
            self._passenger_event(character, player, payment=4000),
            player,
            character,
            http_client_mod=AsyncMock(),
        )

        log = await ServerPassengerArrivedLog.objects.afirst()
        self.assertEqual(log.payment, 4000)
        self.assertIsNone(log.guild_session_id)

    async def test_bonus_not_applied_when_type_filter_fails(self, mock_transfer, mock_floor, mock_wallet, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        mock_floor.return_value = True
        player, character = await self._setup()
        await self._activate_guild_session(
            character, {"allowed_passenger_types": [3], "bonus_pct": 15}
        )

        base_pay, _, _, _ = await process_event(
            self._passenger_event(character, player, passenger_type=2, payment=4000),
            player,
            character,
            http_client_mod=AsyncMock(),
        )

        log = await ServerPassengerArrivedLog.objects.afirst()
        self.assertEqual(log.payment, 4000)
        self.assertIsNone(log.guild_session_id)

    async def test_bonus_applied_when_type_matches(self, mock_transfer, mock_floor, mock_wallet, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        mock_floor.return_value = True
        player, character = await self._setup()
        await self._activate_guild_session(
            character, {"allowed_passenger_types": [2], "bonus_pct": 10}
        )

        base_pay, _, _, _ = await process_event(
            self._passenger_event(character, player, passenger_type=2, payment=5000),
            player,
            character,
            http_client_mod=AsyncMock(),
        )

        log = await ServerPassengerArrivedLog.objects.afirst()
        self.assertEqual(log.payment, 5500)
        self.assertIsNotNone(log.guild_session_id)

    async def test_bonus_applied_after_taxi_bonus(self, mock_transfer, mock_floor, mock_wallet, mock_treasury, mock_rp):
        """Guild bonus applies to the already-adjusted payment (after taxi comfort bonus)."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        mock_floor.return_value = True
        player, character = await self._setup()
        await self._activate_guild_session(character, {"bonus_pct": 10})

        event = {
            "hook": "ServerPassengerArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "PlayerId": str(player.unique_id),
                "Passenger": {
                    "Net_PassengerType": 2,
                    "Net_Payment": 1000,
                    "Net_Distance": 500.0,
                    "Net_bArrived": True,
                    "Net_PassengerFlags": 1,
                    "Net_LCComfortSatisfaction": 3,
                    "Net_StartLocation": {"X": 10, "Y": 10, "Z": 0},
                },
            },
        }

        base_pay, _, _, _ = await process_event(event, player, character, http_client_mod=AsyncMock())

        log = await ServerPassengerArrivedLog.objects.afirst()
        # Taxi bonus: 1000 + 1000 * 3 * 0.2 = 1600
        # Guild bonus: 1600 * 0.10 = 160
        # Total: 1760
        self.assertEqual(log.payment, 1760)
        self.assertIsNotNone(log.guild_session_id)
