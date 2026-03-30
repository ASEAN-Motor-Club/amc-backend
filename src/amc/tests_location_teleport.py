"""Tests for location-based teleport detection in locations.py."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone

from amc.factories import PlayerFactory, CharacterFactory
from amc.models import Delivery, PoliceSession


def _make_ctx(http_client_mod=None, http_client=None):
    """Build a minimal ctx dict for monitor_locations / _check_teleport_by_location."""
    return {
        "http_client_mod": http_client_mod or AsyncMock(),
        "http_client": http_client or AsyncMock(),
    }


@patch("amc.webhook.handle_teleport_or_respawn", new_callable=AsyncMock)
class LocationTeleportDetectionTests(TestCase):
    """Tests for _check_teleport_by_location."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    async def _setup_character(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        return player, character

    async def _deliver_money(self, character, payment=100_000, minutes_ago=0):
        ts = timezone.now() - timedelta(minutes=minutes_ago)
        return await Delivery.objects.acreate(
            timestamp=ts,
            character=character,
            cargo_key="Money",
            quantity=1,
            payment=payment,
        )

    async def test_teleport_detected_with_recent_delivery(self, mock_handle):
        """Player with recent Money delivery moves >100m → penalty triggered."""
        _, character = await self._setup_character()
        await self._deliver_money(character, payment=100_000, minutes_ago=3)

        from amc.locations import _check_teleport_by_location

        old_loc = Point(0, 0, 0)
        new_loc = Point(50_000, 0, 0)  # 50,000 units apart = well over threshold
        ctx = _make_ctx()

        await _check_teleport_by_location(character, old_loc, new_loc, ctx)

        mock_handle.assert_awaited_once()
        # Verify the character arg
        call_args = mock_handle.call_args
        self.assertEqual(call_args[0][1], character)

    async def test_normal_driving_no_penalty(self, mock_handle):
        """Player moves <100m between ticks → no penalty."""
        _, character = await self._setup_character()
        await self._deliver_money(character, payment=100_000, minutes_ago=0)

        from amc.locations import _check_teleport_by_location

        old_loc = Point(0, 0, 0)
        new_loc = Point(5_000, 0, 0)  # 5,000 units < 10,000 threshold
        ctx = _make_ctx()

        await _check_teleport_by_location(character, old_loc, new_loc, ctx)

        mock_handle.assert_not_awaited()

    async def test_no_recent_delivery_no_penalty(self, mock_handle):
        """Teleport detected but no Money deliveries in window → no penalty."""
        _, character = await self._setup_character()
        # Delivery from 15 minutes ago — outside the 10-minute window
        await self._deliver_money(character, payment=100_000, minutes_ago=15)

        from amc.locations import _check_teleport_by_location

        old_loc = Point(0, 0, 0)
        new_loc = Point(50_000, 0, 0)
        ctx = _make_ctx()

        await _check_teleport_by_location(character, old_loc, new_loc, ctx)

        mock_handle.assert_not_awaited()

    async def test_non_money_delivery_no_penalty(self, mock_handle):
        """Teleport with recent non-Money delivery → no penalty."""
        _, character = await self._setup_character()
        await Delivery.objects.acreate(
            timestamp=timezone.now(),
            character=character,
            cargo_key="oranges",
            quantity=1,
            payment=100_000,
        )

        from amc.locations import _check_teleport_by_location

        old_loc = Point(0, 0, 0)
        new_loc = Point(50_000, 0, 0)
        ctx = _make_ctx()

        await _check_teleport_by_location(character, old_loc, new_loc, ctx)

        mock_handle.assert_not_awaited()

    async def test_police_officer_not_penalised(self, mock_handle):
        """Active police officers should not be penalised."""
        _, character = await self._setup_character()
        await self._deliver_money(character, payment=100_000, minutes_ago=0)
        await PoliceSession.objects.acreate(character=character)

        from amc.locations import _check_teleport_by_location

        old_loc = Point(0, 0, 0)
        new_loc = Point(50_000, 0, 0)
        ctx = _make_ctx()

        await _check_teleport_by_location(character, old_loc, new_loc, ctx)

        mock_handle.assert_not_awaited()

    @patch("amc.locations.LOCATION_TELEPORT_DETECTION_ENABLED", False)
    async def test_feature_flag_disabled(self, mock_handle):
        """When feature flag is off → no detection even with valid conditions."""
        _, character = await self._setup_character()
        await self._deliver_money(character, payment=100_000, minutes_ago=0)

        from amc.locations import _check_teleport_by_location

        old_loc = Point(0, 0, 0)
        new_loc = Point(50_000, 0, 0)
        ctx = _make_ctx()

        await _check_teleport_by_location(character, old_loc, new_loc, ctx)

        mock_handle.assert_not_awaited()

    async def test_exactly_at_threshold_no_penalty(self, mock_handle):
        """Distance exactly at threshold → no penalty (uses > not >=)."""
        _, character = await self._setup_character()
        await self._deliver_money(character, payment=100_000, minutes_ago=0)

        from amc.locations import _check_teleport_by_location, TELEPORT_DISTANCE_THRESHOLD

        old_loc = Point(0, 0, 0)
        new_loc = Point(TELEPORT_DISTANCE_THRESHOLD, 0, 0)  # exactly at boundary
        ctx = _make_ctx()

        await _check_teleport_by_location(character, old_loc, new_loc, ctx)

        mock_handle.assert_not_awaited()

    async def test_just_over_threshold_triggers(self, mock_handle):
        """Distance just over threshold → penalty triggered."""
        _, character = await self._setup_character()
        await self._deliver_money(character, payment=100_000, minutes_ago=0)

        from amc.locations import _check_teleport_by_location, TELEPORT_DISTANCE_THRESHOLD

        old_loc = Point(0, 0, 0)
        new_loc = Point(TELEPORT_DISTANCE_THRESHOLD + 1, 0, 0)
        ctx = _make_ctx()

        await _check_teleport_by_location(character, old_loc, new_loc, ctx)

        mock_handle.assert_awaited_once()
