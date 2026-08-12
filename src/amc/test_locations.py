from unittest.mock import AsyncMock, patch
from asgiref.sync import sync_to_async
from datetime import timedelta
from django.contrib.gis.geos import Point, Polygon
from django.test import TestCase
from django.utils import timezone
from amc.models import ShortcutZone
from amc.factories import CharacterFactory
from amc.locations import (
    _check_shortcut_zones,
    SHORTCUT_ZONE_WARNING_MESSAGE,
    SHORTCUT_ZONE_ENTRY_MESSAGE,
)


class ShortcutZoneWarningTests(TestCase):
    """Tests for _check_shortcut_zones proximity warnings."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A 200x200 square polygon centered at (1000, 1000)
        cls.zone_polygon = Polygon(
            ((900, 900), (1100, 900), (1100, 1100), (900, 1100), (900, 900)),
            srid=3857,
        )

    async def _create_zone(self, active=True):
        return await ShortcutZone.objects.acreate(
            name="Test Shortcut",
            polygon=self.zone_polygon,
            active=active,
        )

    def _make_ctx(self, mock_session):
        return {"http_client_mod": mock_session}

    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_warning_on_approach(self, mock_show_popup):
        """Player moves from outside 2000 units to within 2000 units → popup fires."""
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()

        old_loc = Point(-2000, 1000, 0, srid=0)  # 2900 units from polygon edge
        new_loc = Point(-1000, 1000, 0, srid=0)  # 1900 units from polygon edge

        ctx = self._make_ctx(AsyncMock())
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)

        mock_show_popup.assert_called_once_with(
            ctx["http_client_mod"],
            SHORTCUT_ZONE_WARNING_MESSAGE,
            player_id=character.player.unique_id,
        )

    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_no_warning_when_far(self, mock_show_popup):
        """Player stays beyond 2000 units → no popup."""
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()

        old_loc = Point(-3000, 1000, 0, srid=0)  # 3900 units from edge
        new_loc = Point(-2100, 1000, 0, srid=0)  # 3000 units from edge

        ctx = self._make_ctx(AsyncMock())
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)

        mock_show_popup.assert_not_called()

    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_no_warning_when_already_inside(self, mock_show_popup):
        """Player was already within 2000 units → no duplicate warning."""
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()

        old_loc = Point(-1000, 1000, 0, srid=0)  # 1900 units from edge (already close)
        new_loc = Point(-500, 1000, 0, srid=0)  # 1400 units from edge (still close)

        ctx = self._make_ctx(AsyncMock())
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)

        mock_show_popup.assert_not_called()

    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_entry_notification(self, mock_show_popup):
        """Crossing from outside to inside the polygon → entry popup fires."""
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()

        old_loc = Point(-1000, 1000, 0, srid=0)  # 1900 units from edge
        new_loc = Point(1000, 1000, 0, srid=0)  # Inside the polygon

        ctx = self._make_ctx(AsyncMock())
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)

        # Should show entry notification
        mock_show_popup.assert_called_once_with(
            ctx["http_client_mod"],
            SHORTCUT_ZONE_ENTRY_MESSAGE,
            player_id=character.player.unique_id,
        )

    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_inactive_zone_ignored(self, mock_show_popup):
        """Inactive zone should not trigger a warning."""
        await self._create_zone(active=False)
        character = await sync_to_async(CharacterFactory)()

        old_loc = Point(700, 1000, 0, srid=0)
        new_loc = Point(850, 1000, 0, srid=0)

        ctx = self._make_ctx(AsyncMock())
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)

        mock_show_popup.assert_not_called()

    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_taint_set_on_entry_and_not_cleared_on_exit(self, mock_show_popup):
        """Passing through a shortcut zone taints the character for 1h.

        The timestamp is set on entry and MUST persist after leaving all
        zones, so a delivery shortly after passing through is still
        unsubsidised.
        """
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()

        # Enter the zone → taint set
        old_loc = Point(-1000, 1000, 0, srid=0)
        new_loc = Point(1000, 1000, 0, srid=0)
        ctx = self._make_ctx(AsyncMock())
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)
        await character.arefresh_from_db()
        self.assertIsNotNone(character.shortcut_zone_entered_at)

        # Leave all zones → taint MUST persist (NOT cleared on exit)
        old_loc = Point(1000, 1000, 0, srid=0)
        new_loc = Point(-1000, 1000, 0, srid=0)
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)
        await character.arefresh_from_db()
        self.assertIsNotNone(character.shortcut_zone_entered_at)

    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_taint_refreshed_while_inside(self, mock_show_popup):
        """Remaining inside a zone keeps the taint within the 1h window.

        The timestamp is refreshed on every inside tick, so a player camping
        a shortcut zone for >1h stays tainted while physically inside (the
        entry-only path would otherwise let the timestamp go stale).
        """
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()

        # Stay inside the polygon — location updates keep coming
        old_loc = Point(1000, 1000, 0, srid=0)
        new_loc = Point(1002, 1000, 0, srid=0)

        # Simulate an entry timestamp that has already gone stale (>1h old)
        character.shortcut_zone_entered_at = timezone.now() - timedelta(hours=3)
        await character.asave()

        ctx = self._make_ctx(AsyncMock())
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)
        await character.arefresh_from_db()

        stale = timezone.now() - timedelta(hours=3)
        self.assertGreater(character.shortcut_zone_entered_at, stale)

    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_entry_popup_debounced_by_shortcut_zone_entered_at(
        self, mock_show_popup
    ):
        """Re-entering a shortcut zone shortly after leaving doesn't re-popup.

        The entry popup is suppressed while `shortcut_zone_entered_at` is
        still within the popup window, so a player drifting across the
        boundary isn't spammed.
        """
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()
        ctx = self._make_ctx(AsyncMock())

        # First entry — popup fires (no prior taint)
        await _check_shortcut_zones(
            character, Point(-1000, 1000, 0, srid=0), Point(1000, 1000, 0, srid=0), ctx
        )
        mock_show_popup.assert_called_once()

        # Leave, then re-enter almost immediately — taint is recent, no popup
        mock_show_popup.reset_mock()
        await _check_shortcut_zones(
            character, Point(1000, 1000, 0, srid=0), Point(-1000, 1000, 0, srid=0), ctx
        )
        await _check_shortcut_zones(
            character, Point(-1000, 1000, 0, srid=0), Point(1000, 1000, 0, srid=0), ctx
        )
        mock_show_popup.assert_not_called()

    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_entry_popup_fires_after_window_elapses(self, mock_show_popup):
        """Re-entering after the popup window has elapsed triggers the popup again."""
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()
        ctx = self._make_ctx(AsyncMock())

        # First entry → popup
        await _check_shortcut_zones(
            character, Point(-1000, 1000, 0, srid=0), Point(1000, 1000, 0, srid=0), ctx
        )
        mock_show_popup.assert_called_once()

        # Make the taint go stale (older than the popup window)
        character.shortcut_zone_entered_at = timezone.now() - timedelta(minutes=10)
        await character.asave()

        # Leave then re-enter → popup fires again
        mock_show_popup.reset_mock()
        await _check_shortcut_zones(
            character, Point(1000, 1000, 0, srid=0), Point(-1000, 1000, 0, srid=0), ctx
        )
        await _check_shortcut_zones(
            character, Point(-1000, 1000, 0, srid=0), Point(1000, 1000, 0, srid=0), ctx
        )
        mock_show_popup.assert_called_once()
