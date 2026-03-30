"""Tests for the auto-arrest patrol loop (amc.auto_arrest)."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase
from django.utils import timezone

from amc.auto_arrest import _has_recent_money_deliveries, _patrol_tick
from amc.factories import CharacterFactory, PlayerFactory
from amc.models import Confiscation, Delivery, PoliceSession, TeleportPoint

from django.contrib.gis.geos import Point


def _make_player_data(unique_id, character_guid, x, y, z, vehicle=None):
    """Build a fake player dict matching the game server /player/list format."""
    data = {
        "unique_id": str(unique_id),
        "character_guid": character_guid,
        "location": f"X={x} Y={y} Z={z}",
    }
    if vehicle:
        data["vehicle"] = vehicle
    return data


def _make_players_list(player_datas):
    """Wrap player datas into the format returned by get_players()."""
    return [(d["unique_id"], d) for d in player_datas]


@patch("amc.auto_arrest.announce", new_callable=AsyncMock)
@patch("amc.auto_arrest.send_system_message", new_callable=AsyncMock)
@patch("amc.commands.faction.send_fund_to_player_wallet", new_callable=AsyncMock)
@patch("amc.commands.faction.record_confiscation_for_level", new_callable=AsyncMock)
@patch("amc.commands.faction.record_treasury_confiscation_income", new_callable=AsyncMock)
@patch("amc.commands.faction.transfer_money", new_callable=AsyncMock)
@patch("amc.commands.faction.teleport_player", new_callable=AsyncMock)
@patch("amc.commands.faction.force_exit_vehicle", new_callable=AsyncMock)
@patch("amc.commands.faction.show_popup", new_callable=AsyncMock)
class AutoArrestPatrolTests(TestCase):
    """Tests for _patrol_tick auto-arrest behavior."""

    async def _setup_world(self):
        """Create jail teleport point, officer, and criminal."""
        # Create jail teleport point
        await TeleportPoint.objects.acreate(
            name="jail",
            location=Point(0, 0, 0),
        )

        # Create officer with active police session
        officer_player = await sync_to_async(PlayerFactory)()
        officer = await sync_to_async(CharacterFactory)(player=officer_player)
        officer.last_online = timezone.now()
        await officer.asave(update_fields=["last_online"])
        await PoliceSession.objects.acreate(character=officer)

        # Create criminal
        criminal_player = await sync_to_async(PlayerFactory)()
        criminal = await sync_to_async(CharacterFactory)(player=criminal_player)
        criminal.last_online = timezone.now()
        await criminal.asave(update_fields=["last_online"])

        return officer, criminal

    async def test_auto_arrests_suspect_with_money_delivery(
        self, mock_popup, mock_exit_vehicle, mock_teleport, mock_transfer,
        mock_treasury, mock_level, mock_fund_wallet, mock_sys_msg, mock_announce,
    ):
        """Patrol tick arrests a suspect near police who has recent Money deliveries."""
        officer, criminal = await self._setup_world()

        # Criminal has a recent Money delivery
        await Delivery.objects.acreate(
            character=criminal,
            cargo_key="Money",
            quantity=1,
            payment=100_000,
            timestamp=timezone.now() - timedelta(minutes=2),
        )

        # Both players near each other (within 50m = 5000 game units)
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, 1000, 1000, 0),
            _make_player_data(criminal.player.unique_id, criminal.guid, 1500, 1000, 0),
        ])

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players):
            await _patrol_tick(mock_http, mock_http_mod, {})

        # Criminal should have been teleported to jail
        mock_teleport.assert_called_once()
        # Popup shown
        mock_popup.assert_called_once()
        # Confiscation record created
        self.assertEqual(await Confiscation.objects.acount(), 1)
        conf = await Confiscation.objects.afirst()
        self.assertEqual(conf.officer_id, officer.id)
        self.assertEqual(conf.character_id, criminal.id)

    async def test_no_arrest_without_money_delivery(
        self, mock_popup, mock_exit_vehicle, mock_teleport, mock_transfer,
        mock_treasury, mock_level, mock_fund_wallet, mock_sys_msg, mock_announce,
    ):
        """Patrol tick does NOT arrest suspects without recent Money deliveries."""
        officer, criminal = await self._setup_world()

        # Criminal has NO Money deliveries
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, 1000, 1000, 0),
            _make_player_data(criminal.player.unique_id, criminal.guid, 1500, 1000, 0),
        ])

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players):
            await _patrol_tick(mock_http, mock_http_mod, {})

        mock_teleport.assert_not_called()
        self.assertEqual(await Confiscation.objects.acount(), 0)

    async def test_no_arrest_when_out_of_range(
        self, mock_popup, mock_exit_vehicle, mock_teleport, mock_transfer,
        mock_treasury, mock_level, mock_fund_wallet, mock_sys_msg, mock_announce,
    ):
        """Patrol tick does NOT arrest suspects who are too far from police."""
        officer, criminal = await self._setup_world()

        await Delivery.objects.acreate(
            character=criminal,
            cargo_key="Money",
            quantity=1,
            payment=100_000,
            timestamp=timezone.now() - timedelta(minutes=2),
        )

        # Criminal is 100m away (10000 game units > 5000 arrest radius)
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, 1000, 1000, 0),
            _make_player_data(criminal.player.unique_id, criminal.guid, 11000, 1000, 0),
        ])

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players):
            await _patrol_tick(mock_http, mock_http_mod, {})

        mock_teleport.assert_not_called()

    async def test_police_not_arrested(
        self, mock_popup, mock_exit_vehicle, mock_teleport, mock_transfer,
        mock_treasury, mock_level, mock_fund_wallet, mock_sys_msg, mock_announce,
    ):
        """Police officers are never auto-arrested, even if nearby each other."""
        officer, _ = await self._setup_world()

        # Create a second officer nearby
        officer2_player = await sync_to_async(PlayerFactory)()
        officer2 = await sync_to_async(CharacterFactory)(player=officer2_player)
        officer2.last_online = timezone.now()
        await officer2.asave(update_fields=["last_online"])
        await PoliceSession.objects.acreate(character=officer2)

        # Even if officer2 has money deliveries (shouldn't happen, but edge case)
        await Delivery.objects.acreate(
            character=officer2,
            cargo_key="Money",
            quantity=1,
            payment=50_000,
            timestamp=timezone.now() - timedelta(minutes=1),
        )

        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, 1000, 1000, 0),
            _make_player_data(officer2.player.unique_id, officer2.guid, 1500, 1000, 0),
        ])

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players):
            await _patrol_tick(mock_http, mock_http_mod, {})

        mock_teleport.assert_not_called()

    async def test_speed_check_blocks_fast_suspects(
        self, mock_popup, mock_exit_vehicle, mock_teleport, mock_transfer,
        mock_treasury, mock_level, mock_fund_wallet, mock_sys_msg, mock_announce,
    ):
        """Suspects moving too fast in vehicles are NOT auto-arrested."""
        officer, criminal = await self._setup_world()

        await Delivery.objects.acreate(
            character=criminal,
            cargo_key="Money",
            quantity=1,
            payment=100_000,
            timestamp=timezone.now() - timedelta(minutes=2),
        )

        # Previous position (from prior tick)
        from amc.commands.faction import _build_player_locations
        prev_criminal_data = _make_player_data(
            criminal.player.unique_id, criminal.guid, 1000, 1000, 0, vehicle="truck"
        )
        prev_locations = _build_player_locations([(prev_criminal_data["unique_id"], prev_criminal_data)])

        # Current position: criminal moved 2000 units (> 1500 speed limit) in one tick
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, 3000, 1000, 0),
            _make_player_data(criminal.player.unique_id, criminal.guid, 3000, 1000, 0, vehicle="truck"),
        ])

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players):
            await _patrol_tick(mock_http, mock_http_mod, prev_locations)

        mock_teleport.assert_not_called()

    async def test_old_money_delivery_not_arrested(
        self, mock_popup, mock_exit_vehicle, mock_teleport, mock_transfer,
        mock_treasury, mock_level, mock_fund_wallet, mock_sys_msg, mock_announce,
    ):
        """Money deliveries older than the confiscation window don't trigger arrest."""
        officer, criminal = await self._setup_world()

        # Delivery is 15 minutes old (beyond 10 minute window)
        await Delivery.objects.acreate(
            character=criminal,
            cargo_key="Money",
            quantity=1,
            payment=100_000,
            timestamp=timezone.now() - timedelta(minutes=15),
        )

        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, 1000, 1000, 0),
            _make_player_data(criminal.player.unique_id, criminal.guid, 1500, 1000, 0),
        ])

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players):
            await _patrol_tick(mock_http, mock_http_mod, {})

        mock_teleport.assert_not_called()

    async def test_server_announcement_on_arrest(
        self, mock_popup, mock_exit_vehicle, mock_teleport, mock_transfer,
        mock_treasury, mock_level, mock_fund_wallet, mock_sys_msg, mock_announce,
    ):
        """Server announcement fires on auto-arrest."""
        officer, criminal = await self._setup_world()

        await Delivery.objects.acreate(
            character=criminal,
            cargo_key="Money",
            quantity=1,
            payment=100_000,
            timestamp=timezone.now() - timedelta(minutes=2),
        )

        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, 1000, 1000, 0),
            _make_player_data(criminal.player.unique_id, criminal.guid, 1500, 1000, 0),
        ])

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players):
            await _patrol_tick(mock_http, mock_http_mod, {})

        # announce() is called via asyncio.create_task, check it was called
        mock_announce.assert_called_once()
        call_args = mock_announce.call_args[0]
        self.assertIn(officer.name, call_args[0])
        self.assertIn("confiscated", call_args[0])

    async def test_system_message_to_officer(
        self, mock_popup, mock_exit_vehicle, mock_teleport, mock_transfer,
        mock_treasury, mock_level, mock_fund_wallet, mock_sys_msg, mock_announce,
    ):
        """System message is sent to the officer on auto-arrest."""
        officer, criminal = await self._setup_world()

        await Delivery.objects.acreate(
            character=criminal,
            cargo_key="Money",
            quantity=1,
            payment=100_000,
            timestamp=timezone.now() - timedelta(minutes=2),
        )

        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, 1000, 1000, 0),
            _make_player_data(criminal.player.unique_id, criminal.guid, 1500, 1000, 0),
        ])

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.auto_arrest.get_players", new_callable=AsyncMock, return_value=players):
            await _patrol_tick(mock_http, mock_http_mod, {})

        # System message to officer about auto-arrest
        mock_sys_msg.assert_called()
        # Find the call specifically to the officer
        officer_calls = [
            c for c in mock_sys_msg.call_args_list
            if c.kwargs.get("character_guid") == officer.guid
        ]
        self.assertTrue(len(officer_calls) >= 1)
        self.assertIn("auto-arrested", officer_calls[0][0][1])


class HasRecentMoneyDeliveriesTests(TestCase):
    """Unit tests for _has_recent_money_deliveries helper."""

    async def test_returns_true_with_recent_delivery(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await Delivery.objects.acreate(
            character=character,
            cargo_key="Money",
            quantity=1,
            payment=50_000,
            timestamp=timezone.now() - timedelta(minutes=5),
        )
        result = await _has_recent_money_deliveries(character)
        self.assertTrue(result)

    async def test_returns_false_without_delivery(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        result = await _has_recent_money_deliveries(character)
        self.assertFalse(result)

    async def test_returns_false_with_old_delivery(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await Delivery.objects.acreate(
            character=character,
            cargo_key="Money",
            quantity=1,
            payment=50_000,
            timestamp=timezone.now() - timedelta(minutes=15),
        )
        result = await _has_recent_money_deliveries(character)
        self.assertFalse(result)

    async def test_returns_false_with_non_money_delivery(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await Delivery.objects.acreate(
            character=character,
            cargo_key="C::Stone",
            quantity=1,
            payment=50_000,
            timestamp=timezone.now() - timedelta(minutes=2),
        )
        result = await _has_recent_money_deliveries(character)
        self.assertFalse(result)
