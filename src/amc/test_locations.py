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
    SHORTCUT_ZONE_ENTRY_MESSAGE,
    SHORTCUT_ZONE_ENTRY_CHAT_MESSAGE,
)


class ShortcutZoneWarningTests(TestCase):
    """Tests for _check_shortcut_zones proximity warnings."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A 6000x6000 square polygon centered at (1000, 1000) — big enough
        # that the 2000-unit (20m) allowance erosion leaves a core (real
        # zones are 12k-20k units across; 200x200 erodes to nothing).
        cls.zone_polygon = Polygon(
            (
                (-2000, -2000),
                (4000, -2000),
                (4000, 4000),
                (-2000, 4000),
                (-2000, -2000),
            ),
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

    @patch("amc.locations.cache.aget", new_callable=AsyncMock, return_value=None)
    @patch("amc.locations.cache.aset", new_callable=AsyncMock)
    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    @patch("amc.locations.send_system_message", new_callable=AsyncMock)
    async def test_violation_beyond_allowance_escalates_to_popup(
        self, mock_send_msg, mock_show_popup, mock_aset, mock_aget
    ):
        """Deep entry (>20m allowance) → escalation POPUP fires (penalty tier).

        Entry itself is a chat system message; the popup is the escalation.
        """
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()

        old_loc = Point(-12000, 1000, 0, srid=0)  # far outside
        new_loc = Point(1000, 1000, 0, srid=0)  # deep inside (3000 from every edge)

        ctx = self._make_ctx(AsyncMock())
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)

        # Escalation popup fired
        mock_show_popup.assert_called_once_with(
            ctx["http_client_mod"],
            SHORTCUT_ZONE_ENTRY_MESSAGE,
            player_id=character.player.unique_id,
        )
        # Entry chat notice also fired (same tick: outside→deep entry)
        mock_send_msg.assert_called_once_with(
            ctx["http_client_mod"],
            SHORTCUT_ZONE_ENTRY_CHAT_MESSAGE,
            character_guid=character.guid,
        )

    @patch("amc.locations.cache.aget", new_callable=AsyncMock, return_value=None)
    @patch("amc.locations.cache.aset", new_callable=AsyncMock)
    @patch("amc.locations.send_system_message", new_callable=AsyncMock)
    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_edge_touch_within_allowance_tolerated(
        self, mock_show_popup, mock_send_msg, mock_aset, mock_aget
    ):
        """Shallow edge-touch (within the 20m allowance) → entry chat notice only.

        No escalation popup — the penalty tier requires penetrating beyond
        the allowance. Occupancy (taint) is still recorded.
        """
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()

        # Just inside the west edge (x=-2000): depth 800 units < 2000 allowance
        old_loc = Point(-12000, 1000, 0, srid=0)
        new_loc = Point(-1200, 1000, 0, srid=0)

        ctx = self._make_ctx(AsyncMock())
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)

        # Entry chat notice fired
        mock_send_msg.assert_called_once_with(
            ctx["http_client_mod"],
            SHORTCUT_ZONE_ENTRY_CHAT_MESSAGE,
            character_guid=character.guid,
        )
        # No escalation popup — violation tier not reached
        mock_show_popup.assert_not_called()

    @patch("amc.locations.cache.aget", new_callable=AsyncMock, return_value=None)
    @patch("amc.locations.cache.aset", new_callable=AsyncMock)
    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    @patch("amc.locations.send_system_message", new_callable=AsyncMock)
    async def test_violation_popup_debounced_per_player(
        self, mock_send_msg, mock_show_popup, mock_aset, mock_aget
    ):
        """Re-entering the violation depth within the debounce window → no popup.

        The escalation popup is debounced via a Redis cache key per character,
        so a player oscillating at the allowance edge isn't spammed.
        """
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()
        ctx = self._make_ctx(AsyncMock())

        # First deep entry → escalation popup fires, debounce key set
        await _check_shortcut_zones(
            character, Point(-12000, 1000, 0, srid=0), Point(1000, 1000, 0, srid=0), ctx
        )
        mock_show_popup.assert_called_once()
        mock_aset.assert_called_once()

        # Leave, re-enter deep within the window → cache hit → popup suppressed
        mock_show_popup.reset_mock()
        mock_aget.return_value = True
        await _check_shortcut_zones(
            character, Point(1000, 1000, 0, srid=0), Point(-12000, 1000, 0, srid=0), ctx
        )
        await _check_shortcut_zones(
            character, Point(-12000, 1000, 0, srid=0), Point(1000, 1000, 0, srid=0), ctx
        )
        mock_show_popup.assert_not_called()

    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_no_warning_when_far(self, mock_show_popup):
        """Player stays beyond 2000 units → no popup."""
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()

        old_loc = Point(-12000, 1000, 0, srid=0)  # 8000 units from edge
        new_loc = Point(-6100, 1000, 0, srid=0)  # 2100 units from edge

        ctx = self._make_ctx(AsyncMock())
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)

        mock_show_popup.assert_not_called()

    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_no_warning_when_already_inside(self, mock_show_popup):
        """Player was already within 2000 units → no duplicate warning."""
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()

        old_loc = Point(-1000, 1000, 0, srid=0)  # 1000 units from edge (inside allowance band)
        new_loc = Point(-500, 1000, 0, srid=0)  # 1500 units from edge (still in allowance band)

        ctx = self._make_ctx(AsyncMock())
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)

        mock_show_popup.assert_not_called()

    @patch("amc.locations.send_system_message", new_callable=AsyncMock)
    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_entry_notification(self, mock_show_popup, mock_send_msg):
        """Crossing from outside to inside the polygon → entry chat notice fires (no popup).

        Shallow entry (within the 20m allowance) — the escalation popup
        requires penetrating beyond the allowance.
        """
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()

        old_loc = Point(-12000, 1000, 0, srid=0)  # far outside
        new_loc = Point(-1200, 1000, 0, srid=0)  # shallow: 800 units < 2000 allowance

        ctx = self._make_ctx(AsyncMock())
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)

        # Entry = chat system message tier
        mock_send_msg.assert_called_once_with(
            ctx["http_client_mod"],
            SHORTCUT_ZONE_ENTRY_CHAT_MESSAGE,
            character_guid=character.guid,
        )
        mock_show_popup.assert_not_called()

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
        old_loc = Point(-12000, 1000, 0, srid=0)
        new_loc = Point(1000, 1000, 0, srid=0)
        ctx = self._make_ctx(AsyncMock())
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)
        await character.arefresh_from_db()
        self.assertIsNotNone(character.shortcut_zone_entered_at)

        # Leave all zones → taint MUST persist (NOT cleared on exit)
        old_loc = Point(1000, 1000, 0, srid=0)
        new_loc = Point(-12000, 1000, 0, srid=0)
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

    @patch("amc.locations.send_system_message", new_callable=AsyncMock)
    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_entry_notice_debounced_by_shortcut_zone_entered_at(
        self, mock_show_popup, mock_send_msg
    ):
        """Re-entering a shortcut zone shortly after leaving doesn't re-popup.

        The entry popup is suppressed while `shortcut_zone_entered_at` is
        still within the popup window, so a player drifting across the
        boundary isn't spammed.
        """
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()
        ctx = self._make_ctx(AsyncMock())

        # First entry — chat notice fires (no prior taint)
        await _check_shortcut_zones(
            character, Point(-12000, 1000, 0, srid=0), Point(1000, 1000, 0, srid=0), ctx
        )
        mock_send_msg.assert_called_once()

        # Leave, then re-enter almost immediately — taint is recent, no notice
        mock_send_msg.reset_mock()
        await _check_shortcut_zones(
            character, Point(1000, 1000, 0, srid=0), Point(-12000, 1000, 0, srid=0), ctx
        )
        await _check_shortcut_zones(
            character, Point(-12000, 1000, 0, srid=0), Point(1000, 1000, 0, srid=0), ctx
        )
        mock_send_msg.assert_not_called()

    @patch("amc.locations.send_system_message", new_callable=AsyncMock)
    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_entry_notice_fires_after_window_elapses(
        self, mock_show_popup, mock_send_msg
    ):
        """Re-entering after the entry window has elapsed fires the notice again."""
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()
        ctx = self._make_ctx(AsyncMock())

        # First entry → notice
        await _check_shortcut_zones(
            character, Point(-12000, 1000, 0, srid=0), Point(1000, 1000, 0, srid=0), ctx
        )
        mock_send_msg.assert_called_once()

        # Make the taint go stale (older than the entry window)
        character.shortcut_zone_entered_at = timezone.now() - timedelta(minutes=10)
        await character.asave()

        # Leave then re-enter → notice fires again
        mock_send_msg.reset_mock()
        await _check_shortcut_zones(
            character, Point(1000, 1000, 0, srid=0), Point(-12000, 1000, 0, srid=0), ctx
        )
        await _check_shortcut_zones(
            character, Point(-12000, 1000, 0, srid=0), Point(1000, 1000, 0, srid=0), ctx
        )
        mock_send_msg.assert_called_once()
