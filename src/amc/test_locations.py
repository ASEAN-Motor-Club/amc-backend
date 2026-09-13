from unittest.mock import AsyncMock, patch
from asgiref.sync import sync_to_async
from datetime import timedelta
from django.contrib.gis.geos import Point, Polygon
from django.test import TestCase
from django.utils import timezone
from amc.models import Character, ShortcutZone
from amc.factories import CharacterFactory
from amc.locations import (
    _check_shortcut_zones,
    _flush_locations_to_db,
    _shortcut_core_cache,
    SHORTCUT_ZONE_ALLOWANCE_RADIUS,
    SHORTCUT_ZONE_ENTRY_NOTICE_REPEAT_SECONDS,
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

    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    @patch("amc.locations.send_system_message", new_callable=AsyncMock)
    async def test_violation_beyond_allowance_escalates_to_popup(
        self, mock_send_msg, mock_show_popup
    ):
        """Deep entry (>20m allowance) → escalation POPUP fires (penalty tier).

        SAME-TICK DEDUPE: entering already beyond the allowance is the tick
        where a driver at speed arrives (they clear the 20m band within one
        tick), so the popup IS the notice — the chat message must NOT also
        fire, or the player sees two near-identical notices at once (the
        live 2026-09-13 report).
        """
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()

        old_loc = Point(-30000, 1000, 0, srid=0)  # outside the warning band too
        new_loc = Point(1000, 1000, 0, srid=0)  # deep inside (3000 from every edge)

        ctx = self._make_ctx(AsyncMock())
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)

        # Escalation popup fired
        mock_show_popup.assert_called_once_with(
            ctx["http_client_mod"],
            SHORTCUT_ZONE_ENTRY_MESSAGE,
            player_id=character.player.unique_id,
        )
        # ... and NOT the chat notice (one notice per tick)
        mock_send_msg.assert_not_called()

        # Violation depth ⇒ taint set (1h delivery penalty window).
        # (In-memory attribute — prod persists via _flush_locations_to_db.)
        self.assertIsNotNone(character.shortcut_zone_entered_at)

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
        # (approach from far outside, as a real driver would)
        old_loc = Point(-30000, 1000, 0, srid=0)
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

        # NO taint — the 20m buffer is the grace band; turning back is safe.
        # (In-memory attribute: the prod flush is _flush_locations_to_db,
        # which this direct call doesn't run.)
        self.assertIsNone(character.shortcut_zone_entered_at)

    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    @patch("amc.locations.send_system_message", new_callable=AsyncMock)
    async def test_violation_popup_debounced_per_player(
        self, mock_send_msg, mock_show_popup
    ):
        """Re-entering the violation depth while still tainted → no popup.

        The escalation popup's debounce TTL matches the 1h delivery taint
        window in webhook.py: while deliveries are still tainted the player
        already knows; after the taint ages out a fresh violation pops again.
        """
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()
        ctx = self._make_ctx(AsyncMock())

        # First deep entry → escalation popup fires, debounce key set
        await _check_shortcut_zones(
            character, Point(-12000, 1000, 0, srid=0), Point(1000, 1000, 0, srid=0), ctx
        )
        mock_show_popup.assert_called_once()

        # Leave, re-enter deep within the window → popup suppressed
        mock_show_popup.reset_mock()
        await _check_shortcut_zones(
            character, Point(1000, 1000, 0, srid=0), Point(-12000, 1000, 0, srid=0), ctx
        )
        await _check_shortcut_zones(
            character, Point(-12000, 1000, 0, srid=0), Point(1000, 1000, 0, srid=0), ctx
        )
        mock_show_popup.assert_not_called()

    @patch("amc.locations.send_system_message", new_callable=AsyncMock)
    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_no_warning_while_outside_polygon(
        self, mock_show_popup, mock_send_msg
    ):
        """Regression: near the zone but OUTSIDE it → no notice, no popup.

        Proximity must never warn. A player 100 units from the edge but not
        across it gets nothing — reports of "warning while outside the zone"
        came from a proximity band that has been removed.
        """
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()

        old_loc = Point(-2500, 1000, 0, srid=0)  # outside the polygon
        new_loc = Point(-2100, 1000, 0, srid=0)  # still outside, 100 units from the edge

        ctx = self._make_ctx(AsyncMock())
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)

        mock_show_popup.assert_not_called()
        mock_send_msg.assert_not_called()
        self.assertIsNone(character.shortcut_zone_entered_at)

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

        old_loc = Point(-30000, 1000, 0, srid=0)  # outside the 200m band
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
        # Prod persists characters via _flush_locations_to_db (the direct
        # call only mutates the in-memory instance).
        await _flush_locations_to_db([], [character])
        await character.arefresh_from_db()
        self.assertIsNotNone(character.shortcut_zone_entered_at)

        # Leave all zones → taint MUST persist (NOT cleared on exit).
        # NOTE: arefresh_from_db wipes the FK cache — re-fetch with
        # select_related so `character.player` resolves in the async check.
        character = await Character.objects.select_related("player").aget(
            guid=character.guid
        )
        old_loc = Point(1000, 1000, 0, srid=0)
        new_loc = Point(-12000, 1000, 0, srid=0)
        await _check_shortcut_zones(character, old_loc, new_loc, ctx)
        await _flush_locations_to_db([], [character])
        await character.arefresh_from_db()
        self.assertIsNotNone(character.shortcut_zone_entered_at)

    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_taint_refreshed_while_inside(self, mock_show_popup):
        """Remaining inside a zone keeps the taint within the 1h window.

        The timestamp is refreshed on every VIOLATING inside tick, so a
        player camping beyond the buffer for >1h stays tainted while
        physically inside (the entry-only path would otherwise let the
        timestamp go stale).
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
        await _flush_locations_to_db([], [character])
        await character.arefresh_from_db()

        stale = timezone.now() - timedelta(hours=3)
        self.assertGreater(character.shortcut_zone_entered_at, stale)

    @patch("amc.locations.cache.aget", new_callable=AsyncMock, return_value=None)
    @patch("amc.locations.cache.aset", new_callable=AsyncMock)
    @patch("amc.locations.send_system_message", new_callable=AsyncMock)
    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_entry_notice_repeats_every_few_seconds(
        self, mock_show_popup, mock_send_msg, mock_aset, mock_aget
    ):
        """The notice re-sends while the player stays inside the polygon.

        The chat message is visible only ~2 s, so a single send is easily
        missed (freeman, 2026-09-14). Rate-limited to one send per
        SHORTCUT_ZONE_ENTRY_NOTICE_REPEAT_SECONDS per player; the violation
        tier still replaces it on deep ticks (same-tick dedupe).
        """
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()
        ctx = self._make_ctx(AsyncMock())

        # Enter the polygon → notice + rate-limit key set
        await _check_shortcut_zones(
            character, Point(-30000, 1000, 0, srid=0), Point(-1200, 1000, 0, srid=0), ctx
        )
        mock_send_msg.assert_called_once()
        mock_aset.assert_called_once_with(
            f"shortcut_notice:{character.guid}",
            True,
            timeout=SHORTCUT_ZONE_ENTRY_NOTICE_REPEAT_SECONDS,
        )
        # Shallow presence ⇒ no taint, no popup
        self.assertIsNone(character.shortcut_zone_entered_at)
        mock_show_popup.assert_not_called()

        # Still inside within the interval → suppressed (rate limit)
        mock_send_msg.reset_mock()
        mock_aget.return_value = True
        await _check_shortcut_zones(
            character, Point(-1200, 1000, 0, srid=0), Point(-1000, 1000, 0, srid=0), ctx
        )
        mock_send_msg.assert_not_called()

        # Interval elapsed → notice re-sends without a new crossing
        mock_aget.return_value = None
        await _check_shortcut_zones(
            character, Point(-1000, 1000, 0, srid=0), Point(-1100, 1000, 0, srid=0), ctx
        )
        mock_send_msg.assert_called_once()

    @patch("amc.locations.send_system_message", new_callable=AsyncMock)
    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_violation_after_warning_still_escalates(
        self, mock_show_popup, mock_send_msg
    ):
        """The ladder: warn on entry, popup only if they keep going deeper.

        The notice lands on an earlier tick than the violation (shallow entry
        first), so the popup only reaches players who kept going through it.
        """
        await self._create_zone()
        character = await sync_to_async(CharacterFactory)()
        ctx = self._make_ctx(AsyncMock())

        # Tick 1: cross INTO the polygon, still inside the 20m allowance
        # (edge is x=-2000; x=-1500 is 500 units in) → notice only
        await _check_shortcut_zones(
            character, Point(-30000, 1000, 0, srid=0), Point(-1500, 1000, 0, srid=0), ctx
        )
        mock_send_msg.assert_called_once()
        mock_show_popup.assert_not_called()
        self.assertIsNone(character.shortcut_zone_entered_at)

        # Tick 2: keep going, now well past the 20m allowance → popup + taint
        # (the eroded core spans 0..2000, so x=500 is beyond the allowance).
        # The popup replaces the chat on this tick (same-tick dedupe).
        mock_send_msg.reset_mock()
        await _check_shortcut_zones(
            character, Point(-1500, 1000, 0, srid=0), Point(500, 1000, 0, srid=0), ctx
        )
        mock_show_popup.assert_called_once()
        mock_send_msg.assert_not_called()
        self.assertIsNotNone(character.shortcut_zone_entered_at)

    @patch("amc.locations.show_popup", new_callable=AsyncMock)
    async def test_eroded_core_memoised_per_zone(
        self, mock_show_popup
    ):
        """The zone's eroded core is built once per tick, keyed by zone id.

        `buffer()` costs ~50 us and depends only on the zone, so the player
        loop must not rebuild it per player (see _process_location_checks,
        which clears this cache once per tick).
        """
        zone = await self._create_zone()
        character = await sync_to_async(CharacterFactory)()
        ctx = self._make_ctx(AsyncMock())

        _shortcut_core_cache.clear()
        await _check_shortcut_zones(
            character, Point(1000, 1000, 0, srid=0), Point(1002, 1000, 0, srid=0), ctx
        )
        self.assertIn((zone.id, SHORTCUT_ZONE_ALLOWANCE_RADIUS), _shortcut_core_cache)
