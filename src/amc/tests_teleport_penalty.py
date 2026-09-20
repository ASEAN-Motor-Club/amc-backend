"""Tests for teleport heat escalation — wanted players gain heat when teleporting near police."""

import time
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase

from amc.factories import PlayerFactory, CharacterFactory
from amc.models import Wanted, ServerTeleportLog


def _teleport_event(character_guid, hook="ServerTeleportCharacter", seq=100):
    """Build a teleport event with proper dedup fields."""
    return {
        "hook": hook,
        "timestamp": int(time.time()),
        "_seq": seq,
        "_epoch": "test-epoch",
        "data": {
            "CharacterGuid": str(character_guid),
            "AbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
        },
    }


def _mock_nearby_police(distance_units):
    """Return a mock for _get_nearest_police_distance that returns a fixed distance."""
    mock = AsyncMock(return_value=distance_units)
    return mock


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock, return_value=False)
@patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=[])
@patch(
    "amc.webhook.get_treasury_fund_balance",
    new_callable=AsyncMock,
    return_value=100_000,
)
@patch("amc.player_tags.refresh_player_name", new_callable=AsyncMock)
@patch("amc.mod_server.send_system_message", new_callable=AsyncMock)
@patch("amc.game_server.announce", new_callable=AsyncMock)
class TeleportHeatEscalationTests(TestCase):
    """Tests for _handle_teleport_or_respawn — proximity-based heat escalation."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    async def _setup_character(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        return player, character

    async def _process_teleport(
        self, character, hook="ServerTeleportCharacter", seq=100
    ):
        """Run a single teleport event through process_events."""
        from amc.webhook import process_events

        events = [_teleport_event(character.guid, hook=hook, seq=seq)]
        http_client = AsyncMock()
        http_client_mod = AsyncMock()
        await process_events(
            events, http_client=http_client, http_client_mod=http_client_mod
        )

    # ------------------------------------------------------------------
    # No police → no heat
    # ------------------------------------------------------------------

    async def test_teleport_no_police_no_heat(
        self,
        mock_announce,
        mock_system_msg,
        mock_refresh,
        mock_treasury,
        mock_parties,
        mock_rp,
    ):
        """Teleporting while wanted but no police nearby → no heat change."""
        _, character = await self._setup_character()
        initial = 100.0
        await Wanted.objects.acreate(character=character, wanted_remaining=initial)

        await self._process_teleport(character)

        wanted = await Wanted.objects.aget(character=character, expired_at__isnull=True)
        self.assertEqual(wanted.wanted_remaining, initial)

    async def test_teleport_without_wanted_no_effect(
        self,
        mock_announce,
        mock_system_msg,
        mock_refresh,
        mock_treasury,
        mock_parties,
        mock_rp,
    ):
        """Teleporting without Wanted → no heat added."""
        _, character = await self._setup_character()

        await self._process_teleport(character, hook="ServerRespawnCharacter")

        self.assertEqual(
            await Wanted.objects.filter(character=character).acount(), 0
        )

    async def test_teleport_logs_always_created(
        self,
        mock_announce,
        mock_system_msg,
        mock_refresh,
        mock_treasury,
        mock_parties,
        mock_rp,
    ):
        """Teleport log should be created regardless of wanted status."""
        _, character = await self._setup_character()

        await self._process_teleport(character)

        log_count = await ServerTeleportLog.objects.filter(
            character=character
        ).acount()
        self.assertEqual(log_count, 1)

    # ------------------------------------------------------------------
    # Police within 2km → heat applied
    # ------------------------------------------------------------------

    @patch("amc.handlers.teleport._get_nearest_police_distance")
    async def test_teleport_near_police_adds_heat(
        self,
        mock_distance,
        mock_announce,
        mock_system_msg,
        mock_refresh,
        mock_treasury,
        mock_parties,
        mock_rp,
    ):
        """Teleporting with police within 2km should increase wanted_remaining."""
        mock_distance.return_value = 50_000  # 500m — within 2km range

        _, character = await self._setup_character()
        initial = 100.0
        await Wanted.objects.acreate(character=character, wanted_remaining=initial)

        await self._process_teleport(character)

        wanted = await Wanted.objects.aget(character=character, expired_at__isnull=True)
        self.assertGreater(wanted.wanted_remaining, initial)

    @patch("amc.handlers.teleport._get_nearest_police_distance")
    async def test_teleport_police_beyond_2km_no_heat(
        self,
        mock_distance,
        mock_announce,
        mock_system_msg,
        mock_refresh,
        mock_treasury,
        mock_parties,
        mock_rp,
    ):
        """Teleporting with police beyond 2km → no heat change."""
        mock_distance.return_value = 250_000  # 2.5km — outside range

        _, character = await self._setup_character()
        initial = 100.0
        await Wanted.objects.acreate(character=character, wanted_remaining=initial)

        await self._process_teleport(character)

        wanted = await Wanted.objects.aget(character=character, expired_at__isnull=True)
        self.assertEqual(wanted.wanted_remaining, initial)

    @patch("amc.handlers.teleport._get_nearest_police_distance")
    async def test_teleport_point_blank_max_heat(
        self,
        mock_distance,
        mock_announce,
        mock_system_msg,
        mock_refresh,
        mock_treasury,
        mock_parties,
        mock_rp,
    ):
        """Teleporting at point-blank range → max heat (TELEPORT_HEAT_MAX)."""
        from amc.handlers.teleport import TELEPORT_HEAT_MAX

        mock_distance.return_value = 500  # 5m — clamped to MIN_DISTANCE

        _, character = await self._setup_character()
        initial = 100.0
        await Wanted.objects.acreate(character=character, wanted_remaining=initial)

        await self._process_teleport(character)

        wanted = await Wanted.objects.aget(character=character, expired_at__isnull=True)
        self.assertAlmostEqual(
            wanted.wanted_remaining, initial + TELEPORT_HEAT_MAX, places=1
        )

    @patch("amc.handlers.teleport._get_nearest_police_distance")
    async def test_teleport_heat_capped_at_max(
        self,
        mock_distance,
        mock_announce,
        mock_system_msg,
        mock_refresh,
        mock_treasury,
        mock_parties,
        mock_rp,
    ):
        """Heat should not exceed INITIAL_WANTED_LEVEL * 5."""
        mock_distance.return_value = 500  # point-blank

        _, character = await self._setup_character()
        max_heat = Wanted.INITIAL_WANTED_LEVEL * 5
        await Wanted.objects.acreate(
            character=character, wanted_remaining=max_heat - 10
        )

        await self._process_teleport(character)

        wanted = await Wanted.objects.aget(character=character, expired_at__isnull=True)
        self.assertEqual(wanted.wanted_remaining, max_heat)

    @patch("amc.handlers.teleport._get_nearest_police_distance")
    async def test_star_level_change_triggers_name_refresh(
        self,
        mock_distance,
        mock_announce,
        mock_system_msg,
        mock_refresh,
        mock_treasury,
        mock_parties,
        mock_rp,
    ):
        """If star level changes due to heat, player name should be refreshed."""
        mock_distance.return_value = 500  # point-blank → max heat (300)

        _, character = await self._setup_character()
        # Start at 55 (W1), adding ~300 heat → 355 (W5+), star change
        await Wanted.objects.acreate(character=character, wanted_remaining=55)

        await self._process_teleport(character)

        mock_refresh.assert_called()
