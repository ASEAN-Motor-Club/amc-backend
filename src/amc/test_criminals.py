"""Tests for the wanted countdown tick (amc.criminals).

Speed-based wanted law (2026-09 rework, corrected 2026-09-20):
  - Running (>= 50 km/h): wanted GROWS at (S - 50)/50 * A(D) s/s, capped at 5★.
    A(D) = 1.0 at the 500 m near cap, falling to 1/3 far away (speeding far
    builds wanted slower).
  - Hiding (< 50 km/h): wanted DECAYS at (50 - S)/50 * F(D) s/s. F(D) = 1.0 at
    the 500 m near cap, rising to 3.0 far away. Distance NEVER slows or
    freezes decay (the old escape gate is gone): hiding next to a cop clears
    at the plain speed-driven rate.
  - Dormant rule: with zero effective cops on duty (on-duty + online +
    non-AFK) the wanted system is off — every active Wanted record is
    cleared (online suspects via the normal expiry flow, offline silently)
    and organic triggers must not fire. The WantedSystemConfig toggle can
    switch this off (police-independent mode): triggers fire, heat moves,
    and the distance law runs with police distance = infinity.
  - Offline suspects (while armed): no decay, wanted persists indefinitely.
"""

import math
import time
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase
from django.utils import timezone

from amc.criminals import (
    BASE_DECAY_PER_TICK,
    CRIMINAL_SUSPECT_DURATION,
    SCORE_DECAY_FACTOR_PER_TICK,
    TICK_INTERVAL,
    WANTED_NEAR_CAP_UNITS,
    _compute_stars,
    _costume_reconciled_guids,
    _last_compass_sent,
    _last_star_notified,
    _last_suspect_guids,
    active_police_present,
    hide_decay_multiplier,
    initial_heat_for_stars,
    nearest_effective_cop_distance_m,
    refresh_suspect_tags,
    tick_criminal_score_decay,
    tick_police_suspect_locations,
    tick_wanted_countdown,
    wanted_accrual_multiplier,
    wanted_stars_for_delivery,
)
from amc.factories import CharacterFactory, PlayerFactory
from amc.models import (
    CompassTuningConfig,
    PoliceSession,
    Wanted,
    WantedSystemConfig,
)


def _make_player_data(unique_id, character_guid, x, y, z):
    """Build a fake player dict matching the game server /player/list format."""
    return {
        "unique_id": str(unique_id),
        "character_guid": character_guid,
        "location": f"X={x} Y={y} Z={z}",
    }


def _make_players_list(player_datas):
    """Wrap player datas into the format returned by get_players()."""
    return [(d["unique_id"], d) for d in player_datas]


# ---------------------------------------------------------------------------
# Cop and criminal coordinates used across tests
# ---------------------------------------------------------------------------
_SUSPECT_LOC = (5000, 5000, 0)
_COP_CLOSE    = (5000 + 1000, 5000, 0)    # 1000 units = 10m  — inside the 500 m near cap
_COP_MED      = (5000 + 10_000, 5000, 0)  # 10_000 units = 100m — inside the near cap
_COP_REF      = (5000 + Wanted.REF_DISTANCE, 5000, 0)  # at REF_DISTANCE → inside the near cap
_COP_ESCAPED  = (5000 + WANTED_NEAR_CAP_UNITS + 1000, 5000, 0)  # 510m — just past the near cap
_COP_FAR      = (5000 + 100_000, 5000, 0)  # 1000m — past the near cap (F ≈ 1.4)
_COP_3KM      = (5000 + 300_000, 5000, 0)  # 3km — deep in the far band


class ComputeStarsTests(TestCase):
    """Unit tests for _compute_stars helper.

    LEVEL_PER_STAR = INITIAL_WANTED_LEVEL / 5 (e.g. 120), so:
      5 stars: wanted_remaining > 480   (481–600)
      4 stars: wanted_remaining 361–480
      3 stars: wanted_remaining 241–360
      2 stars: wanted_remaining 121–240
      1 star:  wanted_remaining 1–120
      0 stars: wanted_remaining <= 0
    """

    def test_600_is_5_stars(self):
        self.assertEqual(_compute_stars(600), 5)

    def test_481_is_5_stars(self):
        self.assertEqual(_compute_stars(481), 5)

    def test_480_is_4_stars(self):
        self.assertEqual(_compute_stars(480), 4)

    def test_361_is_4_stars(self):
        self.assertEqual(_compute_stars(361), 4)

    def test_360_is_3_stars(self):
        self.assertEqual(_compute_stars(360), 3)

    def test_241_is_3_stars(self):
        self.assertEqual(_compute_stars(241), 3)

    def test_240_is_2_stars(self):
        self.assertEqual(_compute_stars(240), 2)

    def test_121_is_2_stars(self):
        self.assertEqual(_compute_stars(121), 2)

    def test_120_is_1_star(self):
        self.assertEqual(_compute_stars(120), 1)

    def test_1_is_1_star(self):
        self.assertEqual(_compute_stars(1), 1)

    def test_0_is_0_stars(self):
        self.assertEqual(_compute_stars(0), 0)

    def test_negative_is_0_stars(self):
        self.assertEqual(_compute_stars(-10), 0)

    def test_floor_is_1_star(self):
        """A tiny remainder (old escape floor 0.1) still counts as 1 star."""
        self.assertEqual(_compute_stars(0.1), 1)




class DeliveryScaledStarsTests(TestCase):
    """Scale-with-delivery (freeman 2026-09-25): floor 5★, +1 star per full
    $100k of illicit delivery. Stars are meter size + display only."""

    def test_floor_is_5_stars(self):
        for amount in (0, 10_000, 499_999, 500_000):
            self.assertEqual(wanted_stars_for_delivery(amount), 5, amount)

    def test_one_star_per_full_100k(self):
        # the 5★ floor covers the first $500k; 6★ starts at $600k
        self.assertEqual(wanted_stars_for_delivery(600_000), 6)
        self.assertEqual(wanted_stars_for_delivery(800_000), 8)
        self.assertEqual(wanted_stars_for_delivery(1_500_000), 15)
        self.assertEqual(wanted_stars_for_delivery(999_999.99), 9)

    def test_initial_heat_matches_star_bands(self):
        self.assertEqual(initial_heat_for_stars(5), 600)
        self.assertEqual(initial_heat_for_stars(8), 960)

    def test_compute_stars_uncapped_above_5(self):
        self.assertEqual(_compute_stars(960), 8)
        self.assertEqual(_compute_stars(961), 9)
        self.assertEqual(_compute_stars(600), 5)
        self.assertEqual(_compute_stars(360), 3)
        self.assertEqual(_compute_stars(0.1), 1)
        self.assertEqual(_compute_stars(0), 0)

@patch("amc.criminals.refresh_player_name", new_callable=AsyncMock)
@patch("amc.criminals.send_system_message", new_callable=AsyncMock)
class WantedCountdownTickTests(TestCase):
    """Integration tests for tick_wanted_countdown."""

    def setUp(self):
        _last_star_notified.clear()
        _last_suspect_guids.clear()
        # Default to ARMED (cops present) so law tests don't trip the dormant
        # amnesty. Dormant tests set self.armed_mock.return_value = False.
        armed = patch(
            "amc.criminals.active_police_present",
            new_callable=AsyncMock,
            return_value=True,
        )
        self.armed_mock = armed.start()
        self.addCleanup(armed.stop)

    async def _setup_criminal(self, wanted_remaining=300):
        """Create a criminal with wanted status."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
        )
        await character.asave(update_fields=["last_online"])
        await Wanted.objects.acreate(
            character=character,
            wanted_remaining=wanted_remaining,
        )
        return character

    async def _setup_police(self):
        """Create an officer with active police session."""
        player = await sync_to_async(PlayerFactory)()
        officer = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
        )
        await officer.asave(update_fields=["last_online"])
        await PoliceSession.objects.acreate(character=officer)
        return officer

    # -----------------------------------------------------------------------
    # Dormant rule — no effective cops on duty clears everything
    # -----------------------------------------------------------------------

    async def test_dormant_no_cops_clears_online_suspect(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """No effective cops on duty -> wanted is cleared in one tick."""
        self.armed_mock.return_value = False
        criminal = await self._setup_criminal(wanted_remaining=300)
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, 0)
        self.assertIsNotNone(wanted.expired_at)

    async def test_dormant_clears_offline_suspects_too(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Dormant amnesty also expires records of suspects who are offline."""
        self.armed_mock.return_value = False
        criminal = await self._setup_criminal(wanted_remaining=300)

        # Nobody online at all — the suspect is not in the player list
        players = _make_players_list([])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, 0)
        self.assertIsNotNone(wanted.expired_at)
        # Offline suspects are expired silently — no name refresh, no announce
        mock_refresh.assert_not_called()

    async def test_dormant_online_suspect_gets_expiry_flow(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Dormant amnesty: online suspects get the normal expiry flow."""
        self.armed_mock.return_value = False
        criminal = await self._setup_criminal(wanted_remaining=300)
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.announce", new_callable=AsyncMock) as mock_announce:
            await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_refresh.assert_called_once_with(criminal, mock_http_mod)
        mock_announce.assert_awaited_once()
        self.assertIn("no longer wanted", mock_announce.call_args.args[0])

    async def _setup_admin_flag(self, wanted_remaining=600):
        """Create a wanted record set by an admin character (set_by) — the
        shape /setwanted produces."""
        admin_player = await sync_to_async(PlayerFactory)()
        admin = await sync_to_async(CharacterFactory)(player=admin_player)
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
        )
        await character.asave(update_fields=["last_online"])
        await Wanted.objects.acreate(
            character=character,
            wanted_remaining=wanted_remaining,
            set_by=admin,
        )
        return character

    async def test_dormant_preserves_admin_set_wanted(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Admin /setwanted flags survive the dormant amnesty — the command
        is cop-independent, so its output must not be amnestied one tick
        later (freeman 2026-09-20)."""
        self.armed_mock.return_value = False
        flagged = await self._setup_admin_flag(wanted_remaining=600)
        players = _make_players_list(
            [_make_player_data(flagged.player.unique_id, flagged.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(5):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=flagged)
        self.assertEqual(wanted.wanted_remaining, 600)
        self.assertIsNone(wanted.expired_at)
        # No expiry flow ran for the admin flag
        mock_refresh.assert_not_called()

    async def test_dormant_clears_organic_preserves_admin(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """One dormant tick: organic heat is cleared, the admin flag is not."""
        self.armed_mock.return_value = False
        organic_criminal = await self._setup_criminal(wanted_remaining=300)
        flagged = await self._setup_admin_flag(wanted_remaining=600)
        players = _make_players_list([
            _make_player_data(
                organic_criminal.player.unique_id, organic_criminal.guid, *_SUSPECT_LOC
            ),
            _make_player_data(flagged.player.unique_id, flagged.guid, *_SUSPECT_LOC),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.announce", new_callable=AsyncMock):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        organic_wanted = await Wanted.objects.aget(character=organic_criminal)
        self.assertEqual(organic_wanted.wanted_remaining, 0)
        self.assertIsNotNone(organic_wanted.expired_at)

        admin_wanted = await Wanted.objects.aget(character=flagged)
        self.assertEqual(admin_wanted.wanted_remaining, 600)
        self.assertIsNone(admin_wanted.expired_at)

    async def test_admin_flag_frozen_dormant_then_decays_when_armed(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Admin flags freeze while dormant and re-enter the normal speed law
        once a cop is back on duty."""
        flagged = await self._setup_admin_flag(wanted_remaining=200)
        officer = await self._setup_police()
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        # Phase 1: dormant — flag frozen, no decay even while online + still
        self.armed_mock.return_value = False
        players = _make_players_list([
            _make_player_data(flagged.player.unique_id, flagged.guid, *_SUSPECT_LOC),
        ])
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)
        wanted = await Wanted.objects.aget(character=flagged)
        self.assertEqual(wanted.wanted_remaining, 200)
        self.assertIsNone(wanted.expired_at)

        # Phase 2: cop back on duty — normal hiding decay resumes
        # (stationary suspect, cop 1 km away -> F(1000 m) multiplier)
        self.armed_mock.return_value = True
        players_armed = _make_players_list([
            _make_player_data(flagged.player.unique_id, flagged.guid, *_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COP_FAR),
        ])
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players_armed):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)
        wanted = await Wanted.objects.aget(character=flagged)
        self.assertAlmostEqual(
            wanted.wanted_remaining,
            200 - 10 * hide_decay_multiplier(100_000),
            delta=0.5,
        )
        self.assertIsNone(wanted.expired_at)

    # -----------------------------------------------------------------------
    # Police-independent mode (WantedSystemConfig.police_required = OFF)
    # -----------------------------------------------------------------------

    async def _enable_police_independent(self):
        """Flip the admin toggle off: the wanted system runs with zero cops."""
        await WantedSystemConfig.objects.acreate(police_required=False)

    async def test_police_independent_toggle_defaults_on(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        config = await WantedSystemConfig.aget_config()
        self.assertTrue(config.police_required)

    async def test_police_independent_no_cops_no_amnesty_decays_3x(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Toggle OFF + zero cops: no dormant amnesty — the organic wanted
        survives and decays at the police-distance-infinity rate (stationary
        suspect: (50 - 0) * 1/50 * F(inf)=3.0 * 1 s = 3.0 s per tick)."""
        await self._enable_police_independent()
        self.armed_mock.return_value = False
        criminal = await self._setup_criminal(wanted_remaining=300)
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        # active_police_present is never consulted in this mode
        self.armed_mock.assert_not_awaited()
        wanted = await Wanted.objects.aget(character=criminal)
        self.assertAlmostEqual(wanted.wanted_remaining, 297, delta=0.01)
        self.assertIsNone(wanted.expired_at)
        mock_refresh.assert_not_called()

    async def test_police_independent_no_cops_growth_uses_far_mult(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Toggle OFF + zero cops + running suspect: growth uses the
        police-distance-infinity multiplier A = 1/3x."""
        await self._enable_police_independent()
        self.armed_mock.return_value = False
        criminal = await self._setup_criminal(wanted_remaining=300)
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        # 100 km/h in game units/s (speed_units * 0.036 = km/h)
        mgmt_entries = [
            {"CharacterGuid": criminal.guid.upper(), "Speed": 100 / 0.036}
        ]
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.get_players_locations", new_callable=AsyncMock,
                   return_value=mgmt_entries):
            await tick_wanted_countdown(mock_http, mock_http_mod, mock_http_mgmt)

        wanted = await Wanted.objects.aget(character=criminal)
        # (100 - 50) * 1/50 * 1 s * A(inf) = 50 * 0.02 * (1/3) ≈ 0.3333
        self.assertAlmostEqual(wanted.wanted_remaining, 300 + 50 / 50 * (1 / 3), delta=0.01)
        self.assertIsNone(wanted.expired_at)

    async def test_police_independent_toggle_on_restores_dormant_amnesty(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Toggle OFF then ON: the dormant amnesty comes back with zero cops."""
        await self._enable_police_independent()
        config = await WantedSystemConfig.aget_config()
        config.police_required = True
        await config.asave(update_fields=["police_required"])
        self.armed_mock.return_value = False
        criminal = await self._setup_criminal(wanted_remaining=300)
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.announce", new_callable=AsyncMock):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, 0)
        self.assertIsNotNone(wanted.expired_at)

    async def test_offline_suspect_no_decay(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Offline suspects don't decay even when cops are online."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()

        # Only officer is online — criminal not in player list
        players = _make_players_list(
            [_make_player_data(officer.player.unique_id, officer.guid, *_COP_CLOSE)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, 300)
        self.assertIsNone(wanted.expired_at)

    async def test_no_wanted_records_skips_processing(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """If no wanted records exist, nothing happens."""
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        await tick_wanted_countdown(mock_http, mock_http_mod)
        mock_sys_msg.assert_not_called()
        mock_refresh.assert_not_called()

    # -----------------------------------------------------------------------
    # Distance modifiers — far = faster decay (F(D) ≥ 1 everywhere)
    # -----------------------------------------------------------------------

    async def test_cop_distance_accelerates_hiding_decay(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Hiding with a cop 3 km away decays FASTER than with a cop at 100 m."""
        criminal_near = await self._setup_criminal(wanted_remaining=200)
        officer_near = await self._setup_police()

        criminal_far = await self._setup_criminal(wanted_remaining=200)
        officer_far = await self._setup_police()

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        sx, sy, sz = _SUSPECT_LOC

        # Scenario A: cop at 100 m — inside the near cap, base rate
        players_near = _make_players_list([
            _make_player_data(officer_near.player.unique_id, officer_near.guid, *_COP_MED),
            _make_player_data(criminal_near.player.unique_id, criminal_near.guid, sx, sy, sz),
        ])
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players_near):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)
        wanted_near = await Wanted.objects.aget(character=criminal_near)

        # Scenario B: cop at 3 km — F(D) > 1 accelerates decay
        players_far = _make_players_list([
            _make_player_data(officer_far.player.unique_id, officer_far.guid, *_COP_3KM),
            _make_player_data(criminal_far.player.unique_id, criminal_far.guid, sx, sy, sz),
        ])
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players_far):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)
        wanted_far = await Wanted.objects.aget(character=criminal_far)

        # Far cop → faster decay → LOWER remaining
        self.assertLess(wanted_far.wanted_remaining, wanted_near.wanted_remaining)
        # Near cop (inside cap) decays at exactly the base rate
        self.assertAlmostEqual(wanted_near.wanted_remaining, 190, delta=0.1)
        # Far cop matches F(3 km)
        self.assertAlmostEqual(
            wanted_far.wanted_remaining,
            200 - 10 * hide_decay_multiplier(300_000),
            delta=0.5,
        )

    async def test_hiding_point_blank_decays_at_base_rate(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """NO gate: a cop at 10 m does not slow decay at all — base rate."""
        criminal_close = await self._setup_criminal(wanted_remaining=200)
        officer_close = await self._setup_police()

        criminal_med = await self._setup_criminal(wanted_remaining=200)
        officer_med = await self._setup_police()

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        sx, sy, sz = _SUSPECT_LOC

        # Cop at 10 m
        players_close = _make_players_list([
            _make_player_data(officer_close.player.unique_id, officer_close.guid, *_COP_CLOSE),
            _make_player_data(criminal_close.player.unique_id, criminal_close.guid, sx, sy, sz),
        ])
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players_close):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        # Cop at 100 m
        players_med = _make_players_list([
            _make_player_data(officer_med.player.unique_id, officer_med.guid, *_COP_MED),
            _make_player_data(criminal_med.player.unique_id, criminal_med.guid, sx, sy, sz),
        ])
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players_med):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted_close = await Wanted.objects.aget(character=criminal_close)
        wanted_med = await Wanted.objects.aget(character=criminal_med)

        # Both decay at the base rate — distance is clamped inside the cap
        self.assertAlmostEqual(wanted_close.wanted_remaining, 190, delta=0.1)
        self.assertAlmostEqual(wanted_med.wanted_remaining, 190, delta=0.1)
        self.assertIsNone(wanted_close.expired_at)
        self.assertIsNone(wanted_med.expired_at)

    async def test_hiding_just_past_cap_barely_accelerates(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Cop just past the near cap (510 m): F ≈ 1.01, nearly base rate."""
        criminal = await self._setup_criminal(wanted_remaining=200)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_ESCAPED  # 510 m
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        expected = 200 - 10 * hide_decay_multiplier(51_000)
        self.assertAlmostEqual(wanted.wanted_remaining, expected, delta=0.1)
        self.assertIsNone(wanted.expired_at)

    async def test_bounty_does_not_grow_without_police(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Bounty (amount) stays at 0 when no police are nearby."""
        criminal = await self._setup_criminal(wanted_remaining=200)
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(5):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.amount, 0)

    # -----------------------------------------------------------------------
    # Expiry — no floor, wanted can always reach zero
    # -----------------------------------------------------------------------

    async def test_low_wanted_expires_near_cop(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """No floor: wanted at 1.0 s expires while a cop sits at 100 m."""
        criminal = await self._setup_criminal(wanted_remaining=1.0)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_MED  # 100 m — inside the near cap
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, 0)
        self.assertIsNotNone(wanted.expired_at)

    async def test_beyond_near_cap_expires_normally(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Suspect past the near cap decays and can expire."""
        criminal = await self._setup_criminal(wanted_remaining=5.0)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        ex, ey, ez = _COP_ESCAPED  # 510 m
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, ex, ey, ez),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(10):  # 10 ticks × ~1.0/tick > 5 → expires
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, 0)
        self.assertIsNotNone(wanted.expired_at)

    async def test_full_lifecycle_near_then_far(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Full lifecycle: base-rate decay near cops, faster decay once far."""
        criminal = await self._setup_criminal(wanted_remaining=15.0)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_MED  # 100 m — inside the near cap

        near_players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        # Phase 1: near police — base rate (1.0/tick)
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=near_players):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertAlmostEqual(wanted.wanted_remaining, 5.0, delta=0.1)
        self.assertIsNone(wanted.expired_at)

        # Phase 2: cop moves to 3 km → decay accelerates, clears fast
        far_players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, *_COP_3KM),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=far_players):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, 0)
        self.assertIsNotNone(wanted.expired_at)

    # -----------------------------------------------------------------------
    # Speed-based wanted law — growth, F(D) distance acceleration, cap
    # -----------------------------------------------------------------------

    @patch("amc.criminals.get_players_locations", new_callable=AsyncMock)
    async def test_running_at_100kmh_grows_wanted(
        self, mock_locs, mock_sys_msg, mock_refresh,
    ):
        """Suspect moving at 100 km/h → +1.0 s of wanted per tick."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        mock_locs.return_value = [_make_mgmt_entry(criminal.guid, 2778)]  # ≈100 km/h
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod, AsyncMock())

        wanted = await Wanted.objects.aget(character=criminal)
        # 10 ticks × (100 - 50)/50 = +10
        self.assertAlmostEqual(wanted.wanted_remaining, 310, delta=0.5)
        self.assertIsNone(wanted.expired_at)

    @patch("amc.criminals.get_players_locations", new_callable=AsyncMock)
    async def test_growth_capped_at_five_stars(
        self, mock_locs, mock_sys_msg, mock_refresh,
    ):
        """Wanted growth caps at INITIAL_WANTED_LEVEL (5 stars = 600 s)."""
        criminal = await self._setup_criminal(wanted_remaining=595)
        mock_locs.return_value = [_make_mgmt_entry(criminal.guid, 2778)]  # ≈100 km/h
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod, AsyncMock())

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, Wanted.INITIAL_WANTED_LEVEL)

    @patch("amc.criminals.get_players_locations", new_callable=AsyncMock)
    async def test_running_near_cop_still_accrues(
        self, mock_locs, mock_sys_msg, mock_refresh,
    ):
        """The 500 m gate freezes DECAY only — running still accrues near cops."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()
        mock_locs.return_value = [_make_mgmt_entry(criminal.guid, 2778)]  # ≈100 km/h
        sx, sy, sz = _SUSPECT_LOC
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, *_COP_MED),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod, AsyncMock())

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertAlmostEqual(wanted.wanted_remaining, 310, delta=0.5)

    @patch("amc.criminals.get_players_locations", new_callable=AsyncMock)
    async def test_running_far_cop_accrues_slower(
        self, mock_locs, mock_sys_msg, mock_refresh,
    ):
        """Speeding far away builds wanted SLOWER: running at 100 km/h with
        the nearest cop 3 km away accrues at A(3 km) ≈ 0.63× the near rate."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()
        mock_locs.return_value = [_make_mgmt_entry(criminal.guid, 2778)]  # ≈100 km/h
        sx, sy, sz = _SUSPECT_LOC
        players_far = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, *_COP_3KM),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players_far):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod, AsyncMock())

        wanted = await Wanted.objects.aget(character=criminal)
        # 10 ticks × 1.0 × A(3 km) — clearly below the +10 near rate
        expected = 300 + 10 * wanted_accrual_multiplier(300_000)
        self.assertAlmostEqual(wanted.wanted_remaining, expected, delta=0.5)
        self.assertLess(wanted.wanted_remaining, 309)

    @patch("amc.criminals.get_players_locations", new_callable=AsyncMock)
    async def test_far_cop_accelerates_hiding_decay(
        self, mock_locs, mock_sys_msg, mock_refresh,
    ):
        """Cop at 3 km, suspect parked → F(3 km) ≈ 2.11× base decay."""
        criminal = await self._setup_criminal(wanted_remaining=200)
        officer = await self._setup_police()
        mock_locs.return_value = []  # no telemetry → parked
        sx, sy, sz = _SUSPECT_LOC
        far_cop = (5000 + 300_000, 5000, 0)  # 3 km away
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, *far_cop),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod, AsyncMock())

        wanted = await Wanted.objects.aget(character=criminal)
        # 10 ticks × 1.0 × F(2500 m past gate) = 10 × (1 + 2*2500/4500)
        expected = 200 - 10 * hide_decay_multiplier(300_000)
        self.assertAlmostEqual(wanted.wanted_remaining, expected, delta=0.5)

    @patch("amc.criminals.get_players_locations", new_callable=AsyncMock)
    async def test_creep_at_45kmh_decays_slowly(
        self, mock_locs, mock_sys_msg, mock_refresh,
    ):
        """Creeping at 45 km/h decays at (50-45)/50 = 0.1 s/s (no cops)."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        mock_locs.return_value = [_make_mgmt_entry(criminal.guid, 1250)]  # 45 km/h
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod, AsyncMock())

        wanted = await Wanted.objects.aget(character=criminal)
        # 10 ticks × 0.1 × F(no cops = 1.0) = 1.0
        self.assertAlmostEqual(wanted.wanted_remaining, 299, delta=0.5)

    # -----------------------------------------------------------------------
    # Dormant amnesty flow details
    # -----------------------------------------------------------------------

    async def test_no_escape_popup_exists_anymore(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """The gate is gone: hiding near cops just decays, no popups, no stall."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, *_COP_MED),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(5):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertAlmostEqual(wanted.wanted_remaining, 295, delta=0.1)
        # No system messages fired at all (no escape hints in the new law)
        mock_sys_msg.assert_not_called()

    async def test_armed_copless_world_decays_at_base_rate(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Armed but no cop visible in the world snapshot -> base rate decay."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        # F/A fall back to 1.0 when no cop distance is known
        self.assertAlmostEqual(wanted.wanted_remaining, 290, delta=0.1)
        self.assertIsNone(wanted.expired_at)

    # -----------------------------------------------------------------------
    # Star transitions and name refresh
    # -----------------------------------------------------------------------

    async def test_star_change_sends_message(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Crossing a star boundary (5→4) sends the corresponding message to the suspect.

        No police: decay = 1.0/tick.
        Start at 481 (W5). After 1 tick: 481 - 1.0 = 480 (W4).
        """
        criminal = await self._setup_criminal(wanted_remaining=481)

        sx, sy, sz = _SUSPECT_LOC
        # No police — full decay of 1.0/tick
        players = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(1):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        star_calls = [
            c for c in mock_sys_msg.call_args_list
            if len(c.args) > 1 and "4 stars remaining" in c.args[1]
        ]
        self.assertEqual(len(star_calls), 1)
        self.assertEqual(star_calls[0].kwargs["character_guid"], criminal.guid)

    async def test_star_change_refreshes_name(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """refresh_player_name is called when the star count changes."""
        criminal = await self._setup_criminal(wanted_remaining=481)

        sx, sy, sz = _SUSPECT_LOC
        players = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(1):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_refresh.assert_called_once_with(criminal, mock_http_mod)

    async def test_no_message_when_star_unchanged(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """No star-change message sent if wanted decays without crossing a boundary."""
        criminal = await self._setup_criminal(wanted_remaining=500)

        sx, sy, sz = _SUSPECT_LOC
        # No police — 500 - 1.0 = 499 (still W5)
        players = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        # Still W5 → no star-change message at all
        mock_sys_msg.assert_not_called()

    async def test_last_star_notified_cleaned_on_expiry(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """_last_star_notified entry is removed when wanted expires."""
        criminal = await self._setup_criminal(wanted_remaining=0.5)
        officer = await self._setup_police()
        _last_star_notified[criminal.guid] = 1

        sx, sy, sz = _SUSPECT_LOC
        ex, ey, ez = _COP_ESCAPED
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, ex, ey, ez),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        self.assertNotIn(criminal.guid, _last_star_notified)

    async def test_expiry_refreshes_player_name(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """refresh_player_name is called when wanted expires."""
        criminal = await self._setup_criminal(wanted_remaining=0.5)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        ex, ey, ez = _COP_ESCAPED
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, ex, ey, ez),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_refresh.assert_called_once_with(criminal, mock_http_mod)

    async def test_expiry_announces_freedom(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """A public announcement is sent when a criminal's wanted status expires."""
        criminal = await self._setup_criminal(wanted_remaining=0.5)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        ex, ey, ez = _COP_ESCAPED
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, ex, ey, ez),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.announce", new_callable=AsyncMock) as mock_announce:
            await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_announce.assert_awaited_once()
        self.assertIn(criminal.name, mock_announce.call_args.args[0])
        # Organic wanted decaying to zero = evasion wording — but the cop is
        # 510 m away for a single tick, so the message grades to the
        # low-quality tier (freeman 2026-09-26).
        self.assertIn(
            "slipped away from the police without much of a chase",
            mock_announce.call_args.args[0],
        )
        self.assertEqual(mock_announce.call_args.kwargs.get("color"), "43B581")

    # -----------------------------------------------------------------------
    # Underwater auto-arrest
    # -----------------------------------------------------------------------

    async def test_underwater_criminal_gets_arrested(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Criminal below UNDERWATER_Z_THRESHOLD is automatically arrested."""
        from amc.criminals import UNDERWATER_Z_THRESHOLD

        criminal = await self._setup_criminal(wanted_remaining=200)
        sx, sy, _sz = _SUSPECT_LOC
        underwater_z = UNDERWATER_Z_THRESHOLD - 1
        players = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, underwater_z),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with (
            patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players),
            patch("amc.criminals.execute_arrest", new_callable=AsyncMock, return_value=([criminal.name], 1000)) as mock_arrest,
        ):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_arrest.assert_awaited_once()
        call_kwargs = mock_arrest.call_args.kwargs
        self.assertIsNone(call_kwargs["officer_character"])
        self.assertEqual(call_kwargs["http_client"], mock_http)
        self.assertEqual(call_kwargs["http_client_mod"], mock_http_mod)
        # No star-change messages or refresh calls for arrested player
        mock_sys_msg.assert_not_called()
        mock_refresh.assert_not_called()

    async def test_criminal_at_threshold_not_arrested(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Criminal exactly at UNDERWATER_Z_THRESHOLD is not arrested."""
        from amc.criminals import UNDERWATER_Z_THRESHOLD

        criminal = await self._setup_criminal(wanted_remaining=200)
        sx, sy, _sz = _SUSPECT_LOC
        players = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, UNDERWATER_Z_THRESHOLD),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with (
            patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players),
            patch("amc.criminals.execute_arrest", new_callable=AsyncMock) as mock_arrest,
        ):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_arrest.assert_not_called()
        wanted = await Wanted.objects.aget(character=criminal)
        self.assertLess(wanted.wanted_remaining, 200)  # normal decay happened

    async def test_criminal_above_threshold_not_arrested(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Criminal above UNDERWATER_Z_THRESHOLD is not arrested."""
        from amc.criminals import UNDERWATER_Z_THRESHOLD

        criminal = await self._setup_criminal(wanted_remaining=200)
        sx, sy, _sz = _SUSPECT_LOC
        players = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, UNDERWATER_Z_THRESHOLD + 1),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with (
            patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players),
            patch("amc.criminals.execute_arrest", new_callable=AsyncMock) as mock_arrest,
        ):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_arrest.assert_not_called()
        wanted = await Wanted.objects.aget(character=criminal)
        self.assertLess(wanted.wanted_remaining, 200)  # normal decay happened

    async def test_underwater_arrest_failure_does_not_crash_tick(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """If execute_arrest raises, the tick continues for other players."""
        from amc.criminals import UNDERWATER_Z_THRESHOLD

        criminal_a = await self._setup_criminal(wanted_remaining=200)
        criminal_b = await self._setup_criminal(wanted_remaining=200)
        sx, sy, _sz = _SUSPECT_LOC
        underwater_z = UNDERWATER_Z_THRESHOLD - 1
        players = _make_players_list([
            _make_player_data(criminal_a.player.unique_id, criminal_a.guid, sx, sy, underwater_z),
            _make_player_data(criminal_b.player.unique_id, criminal_b.guid, 6000, 6000, 0),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with (
            patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players),
            patch("amc.criminals.execute_arrest", new_callable=AsyncMock, side_effect=ValueError("Jail not configured")) as mock_arrest,
        ):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_arrest.assert_awaited_once()
        # criminal_b should still have decayed normally
        wanted_b = await Wanted.objects.aget(character=criminal_b)
        self.assertLess(wanted_b.wanted_remaining, 200)

    # -----------------------------------------------------------------------
    # Modded-vehicle transition-based despawn
    # -----------------------------------------------------------------------

    async def test_modded_vehicle_already_occupied_no_despawn(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Player already in a modded vehicle when wanted: no despawn."""
        from amc.criminals import _last_modded_vehicle_guids

        criminal = await self._setup_criminal(wanted_remaining=200)
        _last_modded_vehicle_guids.add(criminal.guid)

        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.get_player_last_vehicle", new_callable=AsyncMock, return_value={"vehicle": {"id": 1}}), \
             patch("amc.criminals.get_player_last_vehicle_parts", new_callable=AsyncMock, return_value={"parts": [{"Key": "mod_part", "Slot": 0}]}), \
             patch("amc.criminals.detect_custom_parts", return_value=[{"key": "mod_part"}]), \
             patch("amc.criminals.force_exit_vehicle", new_callable=AsyncMock) as mock_exit, \
             patch("amc.criminals.despawn_player_vehicle", new_callable=AsyncMock) as mock_despawn:
            await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_exit.assert_not_called()
        mock_despawn.assert_not_called()
        wanted = await Wanted.objects.aget(character=criminal)
        self.assertLess(wanted.wanted_remaining, 200)
        self.assertIsNone(wanted.expired_at)

        _last_modded_vehicle_guids.discard(criminal.guid)

    async def test_modded_vehicle_entered_while_wanted_despawn(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Wanted player enters a modded vehicle: vehicle is despawned."""

        criminal = await self._setup_criminal(wanted_remaining=200)

        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.get_player_last_vehicle", new_callable=AsyncMock, return_value={"vehicle": {"id": 1}}), \
             patch("amc.criminals.get_player_last_vehicle_parts", new_callable=AsyncMock, return_value={"parts": [{"Key": "mod_part", "Slot": 0}]}), \
             patch("amc.criminals.detect_custom_parts", return_value=[{"key": "mod_part"}]), \
             patch("amc.criminals.force_exit_vehicle", new_callable=AsyncMock) as mock_exit, \
             patch("amc.criminals.despawn_player_vehicle", new_callable=AsyncMock) as mock_despawn, \
             patch("amc.criminals.show_popup", new_callable=AsyncMock):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_exit.assert_awaited_once()
        mock_despawn.assert_awaited_once()

    async def test_stock_vehicle_after_grace_period_no_arrest(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Wanted record > 2 min old with stock vehicle: no arrest, decays normally."""
        criminal = await self._setup_criminal(wanted_remaining=200)
        wanted = await Wanted.objects.aget(character=criminal)
        wanted.created_at = timezone.now() - timedelta(minutes=3)
        await wanted.asave(update_fields=["created_at"])

        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.get_player_last_vehicle", new_callable=AsyncMock, return_value={"vehicle": {"id": 1}}), \
             patch("amc.criminals.get_player_last_vehicle_parts", new_callable=AsyncMock, return_value={"parts": [{"Key": "stock_part", "Slot": 0}]}), \
             patch("amc.criminals.detect_custom_parts", return_value=[]), \
             patch("amc.criminals.execute_arrest", new_callable=AsyncMock) as mock_arrest:
            await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_arrest.assert_not_called()
        wanted = await Wanted.objects.aget(character=criminal)
        self.assertLess(wanted.wanted_remaining, 200)
        self.assertIsNone(wanted.expired_at)

    async def test_no_vehicle_after_grace_period_no_arrest(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Wanted record > 2 min old with no vehicle: no arrest, decays normally."""
        criminal = await self._setup_criminal(wanted_remaining=200)
        wanted = await Wanted.objects.aget(character=criminal)
        wanted.created_at = timezone.now() - timedelta(minutes=3)
        await wanted.asave(update_fields=["created_at"])

        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.get_player_last_vehicle", new_callable=AsyncMock, return_value={"vehicle": None}), \
             patch("amc.criminals.get_player_last_vehicle_parts", new_callable=AsyncMock, return_value={"parts": []}), \
             patch("amc.criminals.execute_arrest", new_callable=AsyncMock) as mock_arrest:
            await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_arrest.assert_not_called()
        wanted = await Wanted.objects.aget(character=criminal)
        self.assertLess(wanted.wanted_remaining, 200)
        self.assertIsNone(wanted.expired_at)

    async def test_modded_vehicle_check_failure_graceful(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Mod-server failure during mod check is graceful: no crash, decays normally."""
        criminal = await self._setup_criminal(wanted_remaining=200)
        wanted = await Wanted.objects.aget(character=criminal)
        wanted.created_at = timezone.now() - timedelta(minutes=3)
        await wanted.asave(update_fields=["created_at"])

        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.get_player_last_vehicle", new_callable=AsyncMock, side_effect=Exception("mod server down")), \
             patch("amc.criminals.get_player_last_vehicle_parts", new_callable=AsyncMock), \
             patch("amc.criminals.execute_arrest", new_callable=AsyncMock) as mock_arrest:
            await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_arrest.assert_not_called()
        wanted = await Wanted.objects.aget(character=criminal)
        self.assertLess(wanted.wanted_remaining, 200)
        self.assertIsNone(wanted.expired_at)


class ActivePolicePresentTests(TestCase):
    """Unit tests for the dormant-rule gate (active_police_present)."""

    async def _setup_officer(self, last_online_offset=0):
        player = await sync_to_async(PlayerFactory)()
        officer = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now() - timedelta(seconds=last_online_offset),
        )
        await officer.asave(update_fields=["last_online"])
        await PoliceSession.objects.acreate(character=officer)
        return officer

    async def test_no_sessions_is_not_present(self):
        self.assertFalse(await active_police_present(AsyncMock()))

    async def test_stale_session_is_not_present(self):
        await self._setup_officer(last_online_offset=120)
        self.assertFalse(await active_police_present(AsyncMock()))

    async def test_online_non_afk_cop_is_present(self):
        await self._setup_officer()
        mock_mod = AsyncMock()
        with patch("amc.criminals.get_player", new_callable=AsyncMock,
                   return_value={"bAFK": False}):
            self.assertTrue(await active_police_present(mock_mod))

    async def test_afk_only_cops_are_not_present(self):
        await self._setup_officer()
        mock_mod = AsyncMock()
        with patch("amc.criminals.get_player", new_callable=AsyncMock,
                   return_value={"bAFK": True}):
            self.assertFalse(await active_police_present(mock_mod))

    async def test_afk_check_failure_fails_open(self):
        await self._setup_officer()
        mock_mod = AsyncMock()
        with patch("amc.criminals.get_player", new_callable=AsyncMock,
                   side_effect=Exception("mod api down")):
            self.assertTrue(await active_police_present(mock_mod))

    async def test_mixed_afk_state_is_present(self):
        """One AFK cop + one active cop -> still armed."""
        await self._setup_officer()
        player2 = await sync_to_async(PlayerFactory)()
        officer2 = await sync_to_async(CharacterFactory)(
            player=player2,
            last_online=timezone.now(),
        )
        await officer2.asave(update_fields=["last_online"])
        await PoliceSession.objects.acreate(character=officer2)

        mock_mod = AsyncMock()

        async def fake_get_player(session, player_id, force_refresh=False):
            # One cop reports AFK, the other doesn't — order-independent
            return {"bAFK": str(player_id).endswith("0")}

        with patch("amc.criminals.get_player", new=fake_get_player):
            self.assertTrue(await active_police_present(mock_mod))


class NearestEffectiveCopDistanceTests(TestCase):
    """Unit tests for nearest_effective_cop_distance_m (trigger gating).

    Same effective-cop filter as active_police_present, plus position data:
    (False, None) = dormant (no effective cop → no roll); (True, None) =
    present but position unknown → fail open toward the unattenuated roll.
    """

    async def _setup_officer(self, last_online_offset=0):
        player = await sync_to_async(PlayerFactory)()
        officer = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now() - timedelta(seconds=last_online_offset),
        )
        await officer.asave(update_fields=["last_online"])
        await PoliceSession.objects.acreate(character=officer)
        return officer

    async def _setup_criminal(self):
        player = await sync_to_async(PlayerFactory)()
        return await sync_to_async(CharacterFactory)(player=player)

    async def test_no_sessions_is_dormant(self):
        criminal = await self._setup_criminal()
        self.assertEqual(
            await nearest_effective_cop_distance_m(AsyncMock(), AsyncMock(), criminal),
            (False, None),
        )

    async def test_police_independent_mode_is_armed_unattenuated(self):
        """Toggle OFF: zero effective cops does NOT gate the trigger — the
        roll runs unattenuated (= police distance infinity: the attenuation
        is 1.0 beyond 1000 m anyway)."""
        await WantedSystemConfig.objects.acreate(police_required=False)
        criminal = await self._setup_criminal()
        result = await nearest_effective_cop_distance_m(
            AsyncMock(), AsyncMock(), criminal
        )
        self.assertEqual(result, (True, None))

    async def test_inf_distance_saturates_the_law(self):
        """The distance law's limit at police distance = infinity is the
        far band: F = 3.0x decay, A = 1/3x growth, weight w = 1.0."""
        from amc.criminals import _distance_weight

        self.assertEqual(_distance_weight(math.inf), 1.0)
        self.assertEqual(hide_decay_multiplier(math.inf), 3.0)
        self.assertAlmostEqual(wanted_accrual_multiplier(math.inf), 1 / 3)

    async def test_measures_distance_to_nearest_effective_cop(self):
        criminal = await self._setup_criminal()
        cop = await self._setup_officer()
        # 10_000 units apart = 100 m
        players = _make_players_list(
            [
                _make_player_data(cop.player.unique_id, cop.guid, 15_000, 5000, 0),
                _make_player_data(criminal.player.unique_id, criminal.guid, 5000, 5000, 0),
            ]
        )
        with patch("amc.criminals.get_player", new_callable=AsyncMock,
                   return_value={"bAFK": False}), \
             patch("amc.criminals.get_players", new_callable=AsyncMock,
                   return_value=players):
            present, metres = await nearest_effective_cop_distance_m(
                AsyncMock(), AsyncMock(), criminal
            )
        self.assertTrue(present)
        self.assertEqual(metres, 100)

    async def test_nearest_of_two_cops_wins(self):
        criminal = await self._setup_criminal()
        far_cop = await self._setup_officer()
        near_cop = await self._setup_officer()
        players = _make_players_list(
            [
                _make_player_data(far_cop.player.unique_id, far_cop.guid, 15_000, 5000, 0),
                _make_player_data(near_cop.player.unique_id, near_cop.guid, 10_000, 5000, 0),
                _make_player_data(criminal.player.unique_id, criminal.guid, 5000, 5000, 0),
            ]
        )
        with patch("amc.criminals.get_player", new_callable=AsyncMock,
                   return_value={"bAFK": False}), \
             patch("amc.criminals.get_players", new_callable=AsyncMock,
                   return_value=players):
            _, metres = await nearest_effective_cop_distance_m(
                AsyncMock(), AsyncMock(), criminal
            )
        self.assertEqual(metres, 50)

    async def test_afk_cop_is_skipped(self):
        """An AFK cop is not an effective cop — the system stays dormant."""
        criminal = await self._setup_criminal()
        await self._setup_officer()
        with patch("amc.criminals.get_player", new_callable=AsyncMock,
                   return_value={"bAFK": True}):
            result = await nearest_effective_cop_distance_m(
                AsyncMock(), AsyncMock(), criminal
            )
        self.assertEqual(result, (False, None))

    async def test_afk_check_failure_fails_open(self):
        criminal = await self._setup_criminal()
        cop = await self._setup_officer()
        players = _make_players_list(
            [
                _make_player_data(cop.player.unique_id, cop.guid, 15_000, 5000, 0),
                _make_player_data(criminal.player.unique_id, criminal.guid, 5000, 5000, 0),
            ]
        )
        with patch("amc.criminals.get_player", new_callable=AsyncMock,
                   side_effect=Exception("mod api down")), \
             patch("amc.criminals.get_players", new_callable=AsyncMock,
                   return_value=players):
            present, metres = await nearest_effective_cop_distance_m(
                AsyncMock(), AsyncMock(), criminal
            )
        self.assertTrue(present)
        self.assertEqual(metres, 100)

    async def test_position_fetch_failure_fails_open_unattenuated(self):
        criminal = await self._setup_criminal()
        await self._setup_officer()
        with patch("amc.criminals.get_player", new_callable=AsyncMock,
                   return_value={"bAFK": False}), \
             patch("amc.criminals.get_players", new_callable=AsyncMock,
                   side_effect=Exception("game api down")):
            present, metres = await nearest_effective_cop_distance_m(
                AsyncMock(), AsyncMock(), criminal
            )
        self.assertTrue(present)
        self.assertIsNone(metres)

    async def test_criminal_position_unknown_fails_open(self):
        criminal = await self._setup_criminal()
        cop = await self._setup_officer()
        players = _make_players_list(
            [_make_player_data(cop.player.unique_id, cop.guid, 15_000, 5000, 0)]
        )
        with patch("amc.criminals.get_player", new_callable=AsyncMock,
                   return_value={"bAFK": False}), \
             patch("amc.criminals.get_players", new_callable=AsyncMock,
                   return_value=players):
            present, metres = await nearest_effective_cop_distance_m(
                AsyncMock(), AsyncMock(), criminal
            )
        self.assertTrue(present)
        self.assertIsNone(metres)

    async def test_cop_without_position_is_ignored(self):
        """A cop with no position entry doesn't gate the measurement — the
        distance comes from the positioned cops only."""
        criminal = await self._setup_criminal()
        _blind_cop = await self._setup_officer()
        positioned_cop = await self._setup_officer()
        # blind_cop is an effective cop (on-duty session) but is NOT in the
        # game player list — no position entry for it.
        players = _make_players_list(
            [
                _make_player_data(positioned_cop.player.unique_id, positioned_cop.guid, 15_000, 5000, 0),
                _make_player_data(criminal.player.unique_id, criminal.guid, 5000, 5000, 0),
            ]
        )
        with patch("amc.criminals.get_player", new_callable=AsyncMock,
                   return_value={"bAFK": False}), \
             patch("amc.criminals.get_players", new_callable=AsyncMock,
                   return_value=players):
            _, metres = await nearest_effective_cop_distance_m(
                AsyncMock(), AsyncMock(), criminal
            )
        # 10 m blind cop unknown → the 100 m positioned cop decides.
        self.assertEqual(metres, 100)


@patch("amc.criminals.make_suspect", new_callable=AsyncMock)
class RefreshSuspectTagsTests(TestCase):
    """Tests for refresh_suspect_tags — decoupled from tick_wanted_countdown."""

    def setUp(self):
        _last_suspect_guids.clear()
        _costume_reconciled_guids.clear()
        # The costume-reconciliation pass inside refresh_suspect_tags polls
        # get_player_customization for every online criminal; tests that don't
        # stage costume state get a None (clean skip). Costume-specific tests
        # override this with their own narrower patch.
        reconciliation = patch(
            "amc.criminals.get_player_customization",
            new_callable=AsyncMock,
            return_value=None,
        )
        self.reconciliation_mock = reconciliation.start()
        self.addCleanup(reconciliation.stop)

    async def _setup_criminal(self, wanted_remaining=300):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
        )
        await character.asave(update_fields=["last_online"])
        await Wanted.objects.acreate(
            character=character,
            wanted_remaining=wanted_remaining,
        )
        return character

    async def test_calls_make_suspect_for_online_wanted_players(
        self,
        mock_make_suspect,
    ):
        """Online wanted players get make_suspect called."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await refresh_suspect_tags(mock_http_mod)

        expected_duration = math.ceil(300 / BASE_DECAY_PER_TICK * TICK_INTERVAL)
        mock_make_suspect.assert_called_once_with(
            mock_http_mod, criminal.guid, duration_seconds=expected_duration
        )

    async def test_skips_offline_wanted_players(
        self,
        mock_make_suspect,
    ):
        """Offline wanted players (last_online beyond the freshness window)
        are skipped by the wanted-pass query."""
        character = await self._setup_criminal(wanted_remaining=300)
        # Mark as offline per DB (older than ONLINE_THRESHOLD_SECONDS).
        character.last_online = timezone.now() - timedelta(seconds=300)
        await character.asave(update_fields=["last_online"])
        players = _make_players_list([])
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await refresh_suspect_tags(mock_http_mod)

        mock_make_suspect.assert_not_called()

    async def test_skips_expired_wanted_records(
        self,
        mock_make_suspect,
    ):
        """Wanted records with wanted_remaining <= 0 are skipped."""
        criminal = await self._setup_criminal(wanted_remaining=0)
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await refresh_suspect_tags(mock_http_mod)

        mock_make_suspect.assert_not_called()

    async def test_calls_for_multiple_online_players(
        self,
        mock_make_suspect,
    ):
        """Multiple online wanted players all get make_suspect called."""
        criminal_a = await self._setup_criminal(wanted_remaining=300)
        criminal_b = await self._setup_criminal(wanted_remaining=200)
        players = _make_players_list([
            _make_player_data(criminal_a.player.unique_id, criminal_a.guid, *_SUSPECT_LOC),
            _make_player_data(criminal_b.player.unique_id, criminal_b.guid, 6000, 6000, 0),
        ])
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await refresh_suspect_tags(mock_http_mod)

        self.assertEqual(mock_make_suspect.call_count, 2)
        calls = {c.args[1]: c.kwargs["duration_seconds"] for c in mock_make_suspect.call_args_list}
        self.assertEqual(calls[criminal_a.guid], math.ceil(300 / BASE_DECAY_PER_TICK * TICK_INTERVAL))
        self.assertEqual(calls[criminal_b.guid], math.ceil(200 / BASE_DECAY_PER_TICK * TICK_INTERVAL))

    # -------------------------------------------------------------------
    # Costume criminal pass
    # -------------------------------------------------------------------

    async def _setup_costume_criminal(self, wearing_costume=True):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
            wearing_costume=wearing_costume,
            costume_item_key="Costume_Police_01" if wearing_costume else None,
        )
        await character.asave(update_fields=["last_online", "wearing_costume", "costume_item_key"])
        return character

    async def test_costume_criminal_online_gets_suspect(
        self,
        mock_make_suspect,
    ):
        _costume_reconciled_guids.clear()
        character = await self._setup_costume_criminal(wearing_costume=True)
        players = _make_players_list(
            [_make_player_data(character.player.unique_id, character.guid, *_SUSPECT_LOC)]
        )
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=None):
            await refresh_suspect_tags(mock_http_mod)

        costume_calls = [
            c for c in mock_make_suspect.call_args_list
            if c.kwargs.get("duration_seconds") == CRIMINAL_SUSPECT_DURATION
        ]
        self.assertGreaterEqual(len(costume_calls), 1)

    async def test_costume_criminal_not_wearing_no_suspect(
        self,
        mock_make_suspect,
    ):
        _costume_reconciled_guids.clear()
        character = await self._setup_costume_criminal(wearing_costume=False)
        players = _make_players_list(
            [_make_player_data(character.player.unique_id, character.guid, *_SUSPECT_LOC)]
        )
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=None):
            await refresh_suspect_tags(mock_http_mod)

        costume_calls = [
            c for c in mock_make_suspect.call_args_list
            if c.args[1] == character.guid and c.kwargs.get("duration_seconds") == CRIMINAL_SUSPECT_DURATION
        ]
        self.assertEqual(len(costume_calls), 0)

    async def test_costume_criminal_offline_no_suspect(
        self,
        mock_make_suspect,
    ):
        """A costume criminal whose DB last_online is beyond the freshness
        window is filtered out by the costume-pass query — no make_suspect.
        """
        _costume_reconciled_guids.clear()
        character = await self._setup_costume_criminal(wearing_costume=True)
        # Mark as offline per DB (older than ONLINE_THRESHOLD_SECONDS).
        character.last_online = timezone.now() - timedelta(seconds=300)
        await character.asave(update_fields=["last_online"])
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=[]), \
             patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=None):
            await refresh_suspect_tags(mock_http_mod)

        costume_calls = [
            c for c in mock_make_suspect.call_args_list
            if c.args[1] == character.guid and c.kwargs.get("duration_seconds") == CRIMINAL_SUSPECT_DURATION
        ]
        self.assertEqual(len(costume_calls), 0)

    async def test_wanted_and_costume_criminal_called_once(
        self,
        mock_make_suspect,
    ):
        _costume_reconciled_guids.clear()
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
            wearing_costume=True,
            costume_item_key="Costume_Police_01",
        )
        await character.asave(update_fields=["last_online", "wearing_costume", "costume_item_key"])
        await Wanted.objects.acreate(character=character, wanted_remaining=300)

        players = _make_players_list(
            [_make_player_data(character.player.unique_id, character.guid, *_SUSPECT_LOC)]
        )
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=None):
            await refresh_suspect_tags(mock_http_mod)

        guid_calls = [c for c in mock_make_suspect.call_args_list if c.args[1] == character.guid]
        self.assertEqual(len(guid_calls), 1)

    # -------------------------------------------------------------------
    # Every-tick reapplication (mod clamps GE to 60 s; cron fires every 30 s)
    # -------------------------------------------------------------------

    async def test_wanted_only_make_suspect_called_every_tick(
        self,
        mock_make_suspect,
    ):
        """A wanted player (no costume) gets make_suspect called on every
        refresh_suspect_tags tick.  Guards the 30 s reapplication cadence
        required because the mod clamps the GE duration to ~60 s regardless
        of the duration we pass.
        """
        criminal = await self._setup_criminal(wanted_remaining=300)
        players = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)
        ])
        mock_http_mod = AsyncMock()

        # Run 3 consecutive ticks — expect a make_suspect call per tick.
        for _ in range(3):
            with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
                 patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=None):
                await refresh_suspect_tags(mock_http_mod)

        guid_calls = [c for c in mock_make_suspect.call_args_list if c.args[1] == criminal.guid]
        self.assertEqual(len(guid_calls), 3)

    async def test_wanted_with_costume_make_suspect_called_every_tick(
        self,
        mock_make_suspect,
    ):
        """A wanted player who is ALSO wearing a suspect costume still gets
        make_suspect called exactly once per tick (via the wanted pass) and
        does so on every tick — the costume pass must not short-circuit,
        skip, or duplicate the call.
        """
        _costume_reconciled_guids.clear()
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
            wearing_costume=True,
            costume_item_key="Costume_Police_01",
        )
        await character.asave(
            update_fields=["last_online", "wearing_costume", "costume_item_key"]
        )
        await Wanted.objects.acreate(character=character, wanted_remaining=300)

        players = _make_players_list([
            _make_player_data(character.player.unique_id, character.guid, *_SUSPECT_LOC)
        ])
        mock_http_mod = AsyncMock()

        for _ in range(3):
            with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
                 patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=None):
                await refresh_suspect_tags(mock_http_mod)

        guid_calls = [c for c in mock_make_suspect.call_args_list if c.args[1] == character.guid]
        self.assertEqual(len(guid_calls), 3)

    async def test_wanted_only_reapplied_when_mod_player_list_misses(
        self,
        mock_make_suspect,
    ):
        """Regression: the 30 s reapplication must NOT be gated on the mod
        server's /players response.  Even when get_players returns an empty
        list (cache miss, transient API hiccup), a wanted player whose DB
        last_online is fresh still gets make_suspect called.
        """
        criminal = await self._setup_criminal(wanted_remaining=300)
        mock_http_mod = AsyncMock()

        # Simulate 3 ticks where the mod server's player list is empty.
        for _ in range(3):
            with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=[]), \
                 patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=None):
                await refresh_suspect_tags(mock_http_mod)

        guid_calls = [c for c in mock_make_suspect.call_args_list if c.args[1] == criminal.guid]
        self.assertEqual(len(guid_calls), 3)

    async def test_wanted_with_costume_reapplied_when_mod_player_list_misses(
        self,
        mock_make_suspect,
    ):
        """Regression: a wanted+costume player also gets make_suspect
        reapplied on every tick even when the mod's player list is empty.
        Still exactly once per tick (wanted pass takes priority and the
        costume pass must not fire a second time).
        """
        _costume_reconciled_guids.clear()
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
            wearing_costume=True,
            costume_item_key="Costume_Police_01",
        )
        await character.asave(
            update_fields=["last_online", "wearing_costume", "costume_item_key"]
        )
        await Wanted.objects.acreate(character=character, wanted_remaining=300)
        mock_http_mod = AsyncMock()

        for _ in range(3):
            with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=[]), \
                 patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=None):
                await refresh_suspect_tags(mock_http_mod)

        guid_calls = [c for c in mock_make_suspect.call_args_list if c.args[1] == character.guid]
        self.assertEqual(len(guid_calls), 3)

    async def test_reconciliation_hydrates_costume_state(
        self,
        mock_make_suspect,
    ):
        _costume_reconciled_guids.clear()
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
            wearing_costume=False,
            costume_item_key=None,
        )
        await character.asave(update_fields=["last_online", "wearing_costume", "costume_item_key"])

        players = _make_players_list(
            [_make_player_data(character.player.unique_id, character.guid, *_SUSPECT_LOC)]
        )
        mock_http_mod = AsyncMock()
        customization_data = {"Costume": "Costume_Police_01"}

        with patch("amc.criminals.SUSPECT_COSTUMES", frozenset({"Costume_Police_01"})), \
             patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=customization_data):
            await refresh_suspect_tags(mock_http_mod)

        await character.arefresh_from_db()
        self.assertTrue(character.wearing_costume)
        self.assertEqual(character.costume_item_key, "Costume_Police_01")

        costume_calls = [
            c for c in mock_make_suspect.call_args_list
            if c.args[1] == character.guid and c.kwargs.get("duration_seconds") == CRIMINAL_SUSPECT_DURATION
        ]
        self.assertGreaterEqual(len(costume_calls), 1)


class PoliceSuspectLocationsTests(TestCase):
    """Tests for tick_police_suspect_locations."""

    async def _setup_criminal(self, wanted_remaining=300):
        """Create a criminal with wanted status."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
        )
        await character.asave(update_fields=["last_online"])
        await Wanted.objects.acreate(
            character=character,
            wanted_remaining=wanted_remaining,
        )
        return character

    async def _setup_police(self):
        """Create an officer with active police session."""
        player = await sync_to_async(PlayerFactory)()
        officer = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
        )
        await officer.asave(update_fields=["last_online"])
        await PoliceSession.objects.acreate(character=officer)
        return officer

    def _mock_async_iter(self, items):
        """Return an async iterator wrapping a list."""
        async def _iter():
            for item in items:
                yield item
        return _iter()

    async def test_within_200m_close_ping(
        self,
    ):
        """Suspect within 200m: fixed '<200m' proximity ping (no bearing)."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()

        # Suspect 50m away (5000 game units)
        sx, sy, sz = _SUSPECT_LOC
        ox, oy, oz = sx + 5000, sy, sz

        players = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
            _make_player_data(officer.player.unique_id, officer.guid, ox, oy, oz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.get_players_locations", new_callable=AsyncMock, return_value=None), \
             patch("amc.police.get_active_police_characters", return_value=self._mock_async_iter([officer])), \
             patch("amc.criminals.send_system_message", new_callable=AsyncMock) as mock_sys_msg:
            await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_awaited_once()
        message = mock_sys_msg.call_args.args[1]
        self.assertIn("<200m", message)
        self.assertNotIn("m W", message)  # no bearing line

    async def test_beyond_500m_shows_distance_and_direction(
        self,
    ):
        """Suspect beyond 500m shows distance and bearing."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()

        # Suspect 600m away (60000 game units)
        sx, sy, sz = _SUSPECT_LOC
        ox, oy, oz = sx + 60000, sy, sz

        players = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
            _make_player_data(officer.player.unique_id, officer.guid, ox, oy, oz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.get_players_locations", new_callable=AsyncMock, return_value=None), \
             patch("amc.police.get_active_police_characters", return_value=self._mock_async_iter([officer])), \
             patch("amc.criminals.send_system_message", new_callable=AsyncMock) as mock_sys_msg:
            await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_awaited_once()
        message = mock_sys_msg.call_args.args[1]
        self.assertIn("600m", message)
        self.assertIn("W", message)
        self.assertNotIn("within 100m", message)

    async def test_mixed_distances_some_within_some_beyond(
        self,
    ):
        """Multiple suspects at different distances — beyond 500m suspects are shown."""
        criminal_close = await self._setup_criminal(wanted_remaining=300)
        criminal_far = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        # Close suspect: 200m away (within 500m proximity hide)
        cx, cy, cz = sx + 20000, sy, sz
        # Far suspect: 600m away (beyond 500m proximity hide)
        fx, fy, fz = sx + 60000, sy, sz
        # Officer: at suspect origin
        ox, oy, oz = sx, sy, sz

        players = _make_players_list([
            _make_player_data(criminal_close.player.unique_id, criminal_close.guid, cx, cy, cz),
            _make_player_data(criminal_far.player.unique_id, criminal_far.guid, fx, fy, fz),
            _make_player_data(officer.player.unique_id, officer.guid, ox, oy, oz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.get_players_locations", new_callable=AsyncMock, return_value=None), \
             patch("amc.police.get_active_police_characters", return_value=self._mock_async_iter([officer])), \
             patch("amc.criminals.send_system_message", new_callable=AsyncMock) as mock_sys_msg:
            await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_awaited_once()
        message = mock_sys_msg.call_args.args[1]
        # Close suspect shown as the fixed '<200m' proximity ping
        self.assertIn(criminal_close.name, message)
        self.assertIn("<200m", message)
        # Far suspect shown with bearing
        self.assertIn(criminal_far.name, message)
        self.assertIn("600m", message)


@patch("amc.criminals.make_suspect", new_callable=AsyncMock)
class CriminalScoreDecayTests(TestCase):
    """Tests for tick_criminal_score_decay (real-time decay, incl. offline).

    Post-rework there is no AFK/modded-vehicle freeze: the score is a
    progression stat and decays in real time after the grace window.
    """

    def setUp(self):
        _last_suspect_guids.clear()
        _costume_reconciled_guids.clear()

    async def _setup_score(self, score=10000, idle_minutes=None, last_online_min_ago=0):
        """Create a character with a criminal score.

        idle_minutes backdates last_illicit_delivery_at (None = now, inside
        the grace window).
        """
        player = await sync_to_async(PlayerFactory)()
        last_delivery = (
            timezone.now() - timedelta(minutes=idle_minutes)
            if idle_minutes is not None
            else timezone.now()
        )
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now() - timedelta(minutes=last_online_min_ago),
        )
        character.criminal_score = score
        character.last_illicit_delivery_at = last_delivery
        await character.asave(
            update_fields=["last_online", "criminal_score", "last_illicit_delivery_at"]
        )
        return character

    async def test_score_inside_grace_window_does_not_decay(
        self,
        mock_make_suspect,
    ):
        """Scores with a recent illicit delivery (within 48h) are untouched."""
        character = await self._setup_score(score=10000)

        await tick_criminal_score_decay()

        await character.arefresh_from_db(fields=["criminal_score"])
        self.assertEqual(character.criminal_score, 10000)

    async def test_score_decays_after_grace_window(
        self,
        mock_make_suspect,
    ):
        """Scores past the 48h grace decay by the hourly half-life factor."""
        character = await self._setup_score(score=10000, idle_minutes=48 * 60 + 1)

        await tick_criminal_score_decay()

        await character.arefresh_from_db(fields=["criminal_score"])
        self.assertEqual(
            character.criminal_score, int(10000 * SCORE_DECAY_FACTOR_PER_TICK)
        )

    async def test_offline_player_decays(
        self,
        mock_make_suspect,
    ):
        """Decay is real time — offline players decay too."""
        character = await self._setup_score(
            score=10000, idle_minutes=48 * 60 + 1, last_online_min_ago=60
        )

        await tick_criminal_score_decay()

        await character.arefresh_from_db(fields=["criminal_score"])
        self.assertEqual(
            character.criminal_score, int(10000 * SCORE_DECAY_FACTOR_PER_TICK)
        )

    async def test_afk_player_decays(
        self,
        mock_make_suspect,
    ):
        """No AFK freeze post-rework — the score decays regardless."""
        character = await self._setup_score(score=10000, idle_minutes=48 * 60 + 1)

        with patch("amc.mod_server.get_player", new_callable=AsyncMock, return_value={"bAFK": True}):
            await tick_criminal_score_decay()

        await character.arefresh_from_db(fields=["criminal_score"])
        self.assertEqual(
            character.criminal_score, int(10000 * SCORE_DECAY_FACTOR_PER_TICK)
        )

    async def test_modded_vehicle_player_decays(
        self,
        mock_make_suspect,
    ):
        """No modded-vehicle freeze post-rework — the score decays."""
        character = await self._setup_score(score=10000, idle_minutes=48 * 60 + 1)

        await tick_criminal_score_decay()

        await character.arefresh_from_db(fields=["criminal_score"])
        self.assertEqual(
            character.criminal_score, int(10000 * SCORE_DECAY_FACTOR_PER_TICK)
        )

    async def test_score_zeroed_below_floor(
        self,
        mock_make_suspect,
    ):
        """Scores below the decay floor are zeroed (clean slate)."""
        character = await self._setup_score(score=5000, idle_minutes=48 * 60 + 1)

        await tick_criminal_score_decay()

        await character.arefresh_from_db(fields=["criminal_score"])
        self.assertEqual(character.criminal_score, 0)

    async def test_zero_score_is_untouched(
        self,
        mock_make_suspect,
    ):
        """Zero scores stay zero and don't error."""
        character = await self._setup_score(score=0, idle_minutes=48 * 60 + 1)

        await tick_criminal_score_decay()

        await character.arefresh_from_db(fields=["criminal_score"])
        self.assertEqual(character.criminal_score, 0)

    async def test_applies_suspect_to_online_costume_wearers_via_refresh_suspect_tags(
        self,
        mock_make_suspect,
    ):
        """Online costume wearers get make_suspect called via refresh_suspect_tags."""
        _costume_reconciled_guids.clear()
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
            wearing_costume=True,
            costume_item_key="Costume_Police_01",
        )
        await character.asave(update_fields=["last_online", "wearing_costume", "costume_item_key"])

        players = _make_players_list(
            [_make_player_data(character.player.unique_id, character.guid, *_SUSPECT_LOC)]
        )
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=None):
            await refresh_suspect_tags(mock_http_mod)

        mock_make_suspect.assert_any_call(
            mock_http_mod, character.guid, duration_seconds=CRIMINAL_SUSPECT_DURATION
        )

    async def test_skips_suspect_for_offline_costume_wearers_via_refresh_suspect_tags(
        self,
        mock_make_suspect,
    ):
        """Offline costume wearers are not made suspect via refresh_suspect_tags."""
        _costume_reconciled_guids.clear()
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now() - timedelta(minutes=5),
            wearing_costume=True,
            costume_item_key="Costume_Police_01",
        )
        await character.asave(update_fields=["last_online", "wearing_costume", "costume_item_key"])
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=[]), \
             patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=None):
            await refresh_suspect_tags(mock_http_mod)

        costume_calls = [
            c for c in mock_make_suspect.call_args_list
            if c.args[1] == character.guid and c.kwargs.get("duration_seconds") == CRIMINAL_SUSPECT_DURATION
        ]
        self.assertEqual(len(costume_calls), 0)

    async def test_skips_suspect_for_characters_without_guid(
        self,
        mock_make_suspect,
    ):
        """Characters without a guid are not made suspect; the decay tick ignores them safely."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            guid=None,
            last_online=timezone.now(),
        )
        character.criminal_score = 10000
        character.last_illicit_delivery_at = timezone.now() - timedelta(
            minutes=48 * 60 + 1
        )
        await character.asave(
            update_fields=["last_online", "criminal_score", "last_illicit_delivery_at"]
        )

        await tick_criminal_score_decay()

        mock_make_suspect.assert_not_called()


# ---------------------------------------------------------------------------
# clear_suspect integration
# ---------------------------------------------------------------------------

@patch("amc.criminals.clear_suspect", new_callable=AsyncMock)
@patch("amc.criminals.make_suspect", new_callable=AsyncMock)
class ClearSuspectTests(TestCase):
    """Tests for the clear_suspect wiring in criminals.py.

    clear_suspect is emitted in two places:
      1. tick_wanted_countdown — when a wanted record expires naturally and
         the player is online, we proactively drop the in-game suspect GE so
         the blue overlay and Net_Suspects entry disappear immediately rather
         than waiting up to ~70 s for the last-applied GE duration.
      2. refresh_suspect_tags — transition-out pass: GUIDs flagged on the
         previous tick but not the current one are no longer wanted or
         costume criminals, so we clear the GE.
    """

    def setUp(self):
        _last_star_notified.clear()
        _last_suspect_guids.clear()
        _costume_reconciled_guids.clear()
        # These tests exercise the suspect-GE cleanup while ARMED; the
        # dormant amnesty is covered in WantedCountdownTickTests.
        armed = patch(
            "amc.criminals.active_police_present",
            new_callable=AsyncMock,
            return_value=True,
        )
        self.armed_mock = armed.start()
        self.addCleanup(armed.stop)
        # The costume-reconciliation pass in refresh_suspect_tags polls
        # get_player_customization for online criminals; these tests don't
        # stage costume state, so give them a clean None skip.
        reconciliation = patch(
            "amc.criminals.get_player_customization",
            new_callable=AsyncMock,
            return_value=None,
        )
        self.reconciliation_mock = reconciliation.start()
        self.addCleanup(reconciliation.stop)

    async def _setup_criminal(self, wanted_remaining=300):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
        )
        await character.asave(update_fields=["last_online"])
        await Wanted.objects.acreate(
            character=character,
            wanted_remaining=wanted_remaining,
        )
        return character

    # -----------------------------------------------------------------------
    # tick_wanted_countdown → clear_suspect on natural expiry
    # -----------------------------------------------------------------------

    async def test_wanted_expiry_calls_clear_suspect(
        self,
        mock_make_suspect,
        mock_clear_suspect,
    ):
        """When a wanted record expires naturally, clear_suspect is invoked."""
        # Start at 1 tick remaining so a single tick drives it to 0
        criminal = await self._setup_criminal(wanted_remaining=BASE_DECAY_PER_TICK * TICK_INTERVAL)
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.refresh_player_name", new_callable=AsyncMock), \
             patch("amc.criminals.announce", new_callable=AsyncMock):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertIsNotNone(wanted.expired_at)
        mock_clear_suspect.assert_any_await(mock_http_mod, criminal.guid)

    async def test_wanted_not_expired_no_clear(
        self,
        mock_make_suspect,
        mock_clear_suspect,
    ):
        """clear_suspect is not called when the wanted record is still active."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.refresh_player_name", new_callable=AsyncMock):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        mock_clear_suspect.assert_not_called()

    async def test_wanted_expiry_clear_failure_does_not_crash_tick(
        self,
        mock_make_suspect,
        mock_clear_suspect,
    ):
        """A mod-server failure during clear_suspect is logged, not propagated."""
        mock_clear_suspect.side_effect = Exception("mod server down")
        criminal = await self._setup_criminal(wanted_remaining=BASE_DECAY_PER_TICK * TICK_INTERVAL)
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.refresh_player_name", new_callable=AsyncMock), \
             patch("amc.criminals.announce", new_callable=AsyncMock):
            # Should not raise
            await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertIsNotNone(wanted.expired_at)

    # -----------------------------------------------------------------------
    # refresh_suspect_tags → transition-out clears
    # -----------------------------------------------------------------------

    async def test_refresh_suspect_tags_no_prior_flag_no_clear(
        self,
        mock_make_suspect,
        mock_clear_suspect,
    ):
        """First tick with no previously-flagged guids: no clear calls."""
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=[]):
            await refresh_suspect_tags(mock_http_mod)

        mock_clear_suspect.assert_not_called()

    async def test_refresh_suspect_tags_transition_out_calls_clear(
        self,
        mock_make_suspect,
        mock_clear_suspect,
    ):
        """A guid flagged last tick but not this tick gets clear_suspect."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http_mod = AsyncMock()

        # Tick 1: wanted is active → make_suspect called, guid added to
        # _last_suspect_guids.
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await refresh_suspect_tags(mock_http_mod)
        self.assertIn(criminal.guid, _last_suspect_guids)
        mock_clear_suspect.assert_not_called()

        # Expire the wanted record between ticks — this simulates what happens
        # when tick_wanted_countdown zeroes it out (or an external clear).
        await Wanted.objects.filter(character=criminal).aupdate(
            wanted_remaining=0,
            expired_at=timezone.now(),
        )

        # Tick 2: no active wanted, not a costume criminal → clear_suspect
        # called for the transitioned-out guid.
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await refresh_suspect_tags(mock_http_mod)

        mock_clear_suspect.assert_any_await(mock_http_mod, criminal.guid)
        self.assertNotIn(criminal.guid, _last_suspect_guids)

    async def test_refresh_suspect_tags_still_flagged_no_clear(
        self,
        mock_make_suspect,
        mock_clear_suspect,
    ):
        """A guid still in the wanted set across ticks is not cleared."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await refresh_suspect_tags(mock_http_mod)
            await refresh_suspect_tags(mock_http_mod)

        mock_clear_suspect.assert_not_called()
        self.assertIn(criminal.guid, _last_suspect_guids)

    async def test_refresh_suspect_tags_clear_failure_continues(
        self,
        mock_make_suspect,
        mock_clear_suspect,
    ):
        """A clear_suspect failure during transition-out is logged, not raised."""
        criminal_a = await self._setup_criminal(wanted_remaining=300)
        criminal_b = await self._setup_criminal(wanted_remaining=300)
        players = _make_players_list([
            _make_player_data(criminal_a.player.unique_id, criminal_a.guid, *_SUSPECT_LOC),
            _make_player_data(criminal_b.player.unique_id, criminal_b.guid, 6000, 6000, 0),
        ])
        mock_http_mod = AsyncMock()
        mock_clear_suspect.side_effect = Exception("mod server down")

        # Tick 1: both flagged
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await refresh_suspect_tags(mock_http_mod)

        # Expire both wanteds
        await Wanted.objects.filter(
            character__in=[criminal_a, criminal_b],
        ).aupdate(wanted_remaining=0, expired_at=timezone.now())

        # Tick 2: both transition out — clear_suspect raises for each,
        # but the loop continues and the call count reflects both attempts.
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await refresh_suspect_tags(mock_http_mod)

        self.assertEqual(mock_clear_suspect.call_count, 2)

    # -----------------------------------------------------------------------
    # Costume-only criminals are NOT tracked for transition-out clearing
    # (regression tests for the "suspect GE flickers every 10s" bug).
    # -----------------------------------------------------------------------

    async def _setup_costume_only_criminal(self):
        """A criminal wearing a suspect costume but with no wanted record."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
            wearing_costume=True,
            costume_item_key="Costume_Police_01",
        )
        await character.asave(
            update_fields=["last_online", "wearing_costume", "costume_item_key"]
        )
        return character

    async def test_costume_only_guid_not_tracked_in_last_suspect_guids(
        self,
        mock_make_suspect,
        mock_clear_suspect,
    ):
        """A costume-only (non-wanted) GUID is never added to _last_suspect_guids.

        This is the core invariant that prevents the 10 s suspect-GE flicker
        for costume criminals.  Clearing on costume removal is driven by the
        ServerSetEquipmentInventory webhook, not by this tick.
        """
        character = await self._setup_costume_only_criminal()
        players = _make_players_list([
            _make_player_data(character.player.unique_id, character.guid, *_SUSPECT_LOC)
        ])
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=None):
            await refresh_suspect_tags(mock_http_mod)

        # The costume make_suspect call happened …
        mock_make_suspect.assert_any_await(
            mock_http_mod, character.guid, duration_seconds=CRIMINAL_SUSPECT_DURATION,
        )
        # … but the guid must NOT appear in the wanted-only tracking set.
        self.assertNotIn(character.guid, _last_suspect_guids)

    async def test_costume_only_intermittent_locations_miss_no_clear(
        self,
        mock_make_suspect,
        mock_clear_suspect,
    ):
        """Regression: costume criminal dropping out of `locations` on a tick
        does NOT trigger clear_suspect on the next tick.

        Previously, the tick's transition-out pass would diff the combined
        flagged set, so any tick where the mod server's player list omitted
        the GUID would cause clear_suspect to fire on the following tick,
        making the blue suspect GE flicker off every 10 s.
        """
        character = await self._setup_costume_only_criminal()
        players_present = _make_players_list([
            _make_player_data(character.player.unique_id, character.guid, *_SUSPECT_LOC)
        ])
        players_missing = _make_players_list([])
        mock_http_mod = AsyncMock()

        # Tick 1: player present → costume pass applies make_suspect.
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players_present), \
             patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=None):
            await refresh_suspect_tags(mock_http_mod)

        # Tick 2: locations map misses the GUID (transient mod-server miss).
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players_missing), \
             patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=None):
            await refresh_suspect_tags(mock_http_mod)

        # Tick 3: player reappears.
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players_present), \
             patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=None):
            await refresh_suspect_tags(mock_http_mod)

        # clear_suspect must never have fired for this costume-only GUID.
        mock_clear_suspect.assert_not_called()

    async def test_costume_only_last_online_lag_no_clear(
        self,
        mock_make_suspect,
        mock_clear_suspect,
    ):
        """Regression: costume criminal whose `last_online` falls outside the
        60 s window on a tick does NOT trigger clear_suspect on the next tick.
        """
        character = await self._setup_costume_only_criminal()
        players = _make_players_list([
            _make_player_data(character.player.unique_id, character.guid, *_SUSPECT_LOC)
        ])
        mock_http_mod = AsyncMock()

        # Tick 1: fresh last_online → costume pass applies make_suspect.
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=None):
            await refresh_suspect_tags(mock_http_mod)

        # Push last_online outside the 60 s window — simulates mod-server lag
        # in updating the character's heartbeat.
        character.last_online = timezone.now() - timedelta(minutes=5)
        await character.asave(update_fields=["last_online"])

        # Tick 2: character is excluded from the costume queryset.
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=None):
            await refresh_suspect_tags(mock_http_mod)

        # clear_suspect must never have fired for this costume-only GUID.
        mock_clear_suspect.assert_not_called()

    async def test_wanted_and_costume_criminal_wanted_cleared_preserves_suspect(
        self,
        mock_make_suspect,
        mock_clear_suspect,
    ):
        """A player who is both wanted AND wearing a costume: when the wanted
        record is cleared externally, the next tick's transition-out pass
        must NOT call clear_suspect because the player is still a costume
        suspect.  The costume pass re-applies make_suspect to keep the GE
        alive with no visible gap in the blue overlay.
        """
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
            wearing_costume=True,
            costume_item_key="Costume_Police_01",
        )
        await character.asave(
            update_fields=["last_online", "wearing_costume", "costume_item_key"]
        )
        await Wanted.objects.acreate(character=character, wanted_remaining=300)

        players = _make_players_list([
            _make_player_data(character.player.unique_id, character.guid, *_SUSPECT_LOC)
        ])
        mock_http_mod = AsyncMock()

        # Tick 1: wanted → guid flagged in _last_suspect_guids via wanted pass.
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=None):
            await refresh_suspect_tags(mock_http_mod)
        self.assertIn(character.guid, _last_suspect_guids)

        # Clear the wanted record externally (e.g. admin command).
        await Wanted.objects.filter(character=character).aupdate(
            wanted_remaining=0, expired_at=timezone.now(),
        )

        # Tick 2: no active wanted, but costume still on → costume pass
        # re-applies make_suspect and the transition-out diff (wanted |
        # costume) keeps the GE alive.
        mock_clear_suspect.reset_mock()
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=None):
            await refresh_suspect_tags(mock_http_mod)

        mock_clear_suspect.assert_not_called()
        # make_suspect was re-applied via the costume pass.
        mock_make_suspect.assert_any_await(
            mock_http_mod, character.guid, duration_seconds=CRIMINAL_SUSPECT_DURATION,
        )
        # The guid is NOT tracked in _last_suspect_guids (costume-only path
        # doesn't add to the set — see module comment on _last_suspect_guids).
        self.assertNotIn(character.guid, _last_suspect_guids)


def _make_mgmt_entry(character_guid, speed):
    """Build a fake entry matching get_players_locations() output format."""
    return {
        "CharacterGuid": character_guid.upper(),
        "Location": {"X": 0, "Y": 0, "Z": 0},
        "VehicleKey": None,
        "Yaw": 0,
        "Speed": speed,
        "Velocity": {"X": 0, "Y": 0, "Z": 0},
        "RPM": 0,
        "Gear": 0,
    }


class _AsyncList:
    """Wrap a list to support ``async for`` iteration (mimics a Django QuerySet)."""

    def __init__(self, items):
        self._items = items

    def __aiter__(self):
        return self._AsyncIter(self._items)

    class _AsyncIter:
        def __init__(self, items):
            self._iter = iter(items)

        async def __anext__(self):
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration


# Suspect at (50000, 50000, 0) — ~707m from origin
_COMPASS_SUSPECT_LOC = (50000, 50000, 0)
# Officer placements at exact distances from the suspect (positive X axis):
_COMPASS_COP_CLOSE = (50000 + 10_000, 50000, 0)    # 100 m — inside the silence ring
_COMPASS_COP_600M = (50000 + 60_000, 50000, 0)    # 600 m
_COMPASS_COP_1KM = (50000 + 100_000, 50000, 0)    # 1 km
_COMPASS_COP_3KM = (50000 + 300_000, 50000, 0)    # 3 km


@patch("amc.criminals.send_system_message", new_callable=AsyncMock)
@patch("amc.police.get_active_police_characters", new_callable=AsyncMock)
@patch("amc.criminals.get_players_locations", new_callable=AsyncMock)
@patch("amc.criminals.get_players", new_callable=AsyncMock)
class CompassTickTests(TestCase):
    """Tests for tick_police_suspect_locations under the per-officer
    speed-and-distance cadence:

        solo = 1 / (D * (S + 20 km/h) * COMPASS_C)
        clamped to [COMPASS_MIN_INTERVAL, COMPASS_MAX_INTERVAL]
        effective = solo × min(N, 2)   (N = officers beyond their own ring)

    An officer inside the suspect's close close ring receives a fixed
    "<ring>m proximity ping (every max_interval, no bearing, not
    counted into the budget); each officer's cadence is keyed on their own
    distance and
    split across the receiving force (force budget, capped at 2 so a large
    response is never slower per cop than a pair).
    """

    def setUp(self):
        _last_compass_sent.clear()

    async def _setup_criminal(self, wanted_remaining=300):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
        )
        await character.asave(update_fields=["last_online"])
        await Wanted.objects.acreate(
            character=character,
            wanted_remaining=wanted_remaining,
        )
        return character

    async def _setup_police(self):
        player = await sync_to_async(PlayerFactory)()
        officer = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
        )
        await officer.asave(update_fields=["last_online"])
        await PoliceSession.objects.acreate(character=officer)
        return officer

    # -------------------------------------------------------------------
    # Early returns
    # -------------------------------------------------------------------

    async def test_no_wanted_records_clears_compass_state(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """No wanted records → early return, _last_compass_sent cleared."""
        _last_compass_sent[("cop", "suspect")] = 999.0
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_get_players.assert_not_called()
        mock_sys_msg.assert_not_called()
        self.assertEqual(_last_compass_sent, {})

    async def test_no_native_players_returns_early(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """get_players returns None → early return, no messages."""
        await self._setup_criminal()
        mock_get_players.return_value = None
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_not_called()

    async def test_empty_native_players_returns_early(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """get_players returns empty list → early return."""
        await self._setup_criminal()
        mock_get_players.return_value = []
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_not_called()

    async def test_suspect_not_in_native_locations_skipped(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Wanted character not in native player list → skipped."""
        await self._setup_criminal()
        officer = await self._setup_police()

        # Only the officer is in the native player list
        mock_get_players.return_value = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_1KM),
        ])
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_not_called()

    async def test_no_officers_online_returns_early(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """No police officers online → early return, no messages."""
        criminal = await self._setup_criminal()
        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
        ])
        mock_police.return_value = _AsyncList([])
        mock_get_locations.return_value = []
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_not_called()

    # -------------------------------------------------------------------
    # Cadence law — interval values at representative D x S points
    # -------------------------------------------------------------------

    async def test_stationary_600m_interval_clamped_15s(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Stationary suspect at 600 m: raw 1/(600*20*C) ≈ 55.6 s → clamped
        to the 15 s ceiling."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_600M),
        ])
        mock_get_locations.return_value = []
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        # First tick — sends (last_sent = 0)
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_awaited_once()

        # 13 s since last send → not yet
        _last_compass_sent[(officer.guid, criminal.guid)] = time.monotonic() - 13
        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_not_called()

        # 16 s since last send → sends again
        _last_compass_sent[(officer.guid, criminal.guid)] = time.monotonic() - 16
        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_awaited_once()

    async def test_stationary_1km_interval_clamped_15s(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Stationary suspect at 1 km → raw 1/(1000*20*C) ≈ 33.3 s → clamped
        to the 15 s ceiling."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_1KM),
        ])
        mock_get_locations.return_value = []
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        # First tick — sends
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_awaited_once()

        # 13 s since last send → not yet
        _last_compass_sent[(officer.guid, criminal.guid)] = time.monotonic() - 13
        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_not_called()

        # 16 s since last send → sends again
        _last_compass_sent[(officer.guid, criminal.guid)] = time.monotonic() - 16
        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_awaited_once()

    async def test_fast_far_clamped_to_3s(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """3 km + 200 km/h → base 5.6 s × 20/220 ≈ 0.5 → floored at 3 s."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_3KM),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 5556),  # ≈200 km/h
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_awaited_once()

        # 2 s since last send → not yet
        _last_compass_sent[(officer.guid, criminal.guid)] = time.monotonic() - 2
        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_not_called()

        # 4 s since last send → sends again
        _last_compass_sent[(officer.guid, criminal.guid)] = time.monotonic() - 4
        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_awaited_once()

    async def test_speed_speeds_up_updates_at_same_distance(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """1 km out: stationary clamps to 15 s, an 80 km/h suspect updates in 3 s."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_1KM),
        ])
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        # Stationary: 10 s since last send → within the 15 s ceiling
        mock_get_locations.return_value = []
        _last_compass_sent[(officer.guid, criminal.guid)] = time.monotonic() - 10
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_not_called()

        # 80 km/h: same 10 s since last send → past the 3 s interval
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 2222),  # ≈80 km/h
        ]
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_awaited_once()

    # -------------------------------------------------------------------
    # Per-officer silence ring and per-officer cadence
    # -------------------------------------------------------------------

    async def test_officer_inside_ring_close_ping_far_officer_receives(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Officer at 100 m gets the fixed close ping (no bearing);
        officer at 3 km still gets the normal bearing line."""
        criminal = await self._setup_criminal()
        officer_near = await self._setup_police()
        officer_far = await self._setup_police()

        # Widen the ring via the live tuning row: label must follow it
        tuning = await CompassTuningConfig.aget_active()
        tuning.ring_distance = 30_000  # 300 m
        await tuning.asave(update_fields=["ring_distance"])

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer_near.player.unique_id, officer_near.guid, *_COMPASS_COP_CLOSE),
            _make_player_data(officer_far.player.unique_id, officer_far.guid, *_COMPASS_COP_3KM),
        ])
        mock_get_locations.return_value = []
        mock_police.return_value = _AsyncList([officer_near, officer_far])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        # Two messages — the close ping to the near officer, the bearing to
        # the far one
        self.assertEqual(mock_sys_msg.await_count, 2)
        guids = {c.kwargs.get("character_guid")
                 for c in mock_sys_msg.await_args_list}
        self.assertEqual(guids, {officer_near.guid, officer_far.guid})
        near_call = next(c for c in mock_sys_msg.await_args_list
                         if c.kwargs.get("character_guid") == officer_near.guid)
        self.assertIn("<300m", near_call.args[1])

    async def test_live_tuning_override_changes_cadence(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Editing the admin singleton changes the cadence on the next tick:
        max_interval 6 s → a parked 1 km suspect updates every 6 s."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_1KM),
        ])
        mock_get_locations.return_value = []
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        tuning = await CompassTuningConfig.aget_active()
        tuning.max_interval = 6.0
        await tuning.asave(update_fields=["max_interval"])

        # t-5: within the live 6 s interval → silent
        _last_compass_sent[(officer.guid, criminal.guid)] = time.monotonic() - 5
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_not_awaited()

        # t-7: past the live interval → sends
        _last_compass_sent[(officer.guid, criminal.guid)] = time.monotonic() - 7
        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_awaited_once()

    async def test_per_officer_cadence_independent(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Two officers, both >200 m: pair keys throttle independently —
        with the force budget each interval is solo × N(=2): the 1 km
        officer (30 s effective) stays quiet at t-25 while the 3 km
        officer (11.1 s effective) receives."""
        criminal = await self._setup_criminal()
        officer_1km = await self._setup_police()
        officer_3km = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer_1km.player.unique_id, officer_1km.guid, *_COMPASS_COP_1KM),
            _make_player_data(officer_3km.player.unique_id, officer_3km.guid, *_COMPASS_COP_3KM),
        ])
        mock_get_locations.return_value = []
        mock_police.return_value = _AsyncList([officer_1km, officer_3km])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        now = time.monotonic()
        _last_compass_sent[(officer_1km.guid, criminal.guid)] = now - 25
        _last_compass_sent[(officer_3km.guid, criminal.guid)] = now - 25

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_awaited_once()
        self.assertEqual(mock_sys_msg.await_args.kwargs.get("character_guid"), officer_3km.guid)

    # -------------------------------------------------------------------
    # Force budget — N receiving cops split one cop's cadence
    # -------------------------------------------------------------------

    async def test_two_cops_share_one_budget(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Two officers both at 1 km, stationary suspect: solo 15 s × N(=2)
        → each waits 30 s. Both fire on first contact, then neither until
        the effective interval elapses."""
        criminal = await self._setup_criminal()
        officer_a = await self._setup_police()
        officer_b = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer_a.player.unique_id, officer_a.guid, *_COMPASS_COP_1KM),
            _make_player_data(officer_b.player.unique_id, officer_b.guid, *_COMPASS_COP_1KM),
        ])
        mock_get_locations.return_value = []
        mock_police.return_value = _AsyncList([officer_a, officer_b])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        # First tick — both receive (no prior state)
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        self.assertEqual(mock_sys_msg.await_count, 2)

        # 25 s since last send: past the SOLO 15 s but inside 2 × 15 = 30 s
        now = time.monotonic()
        _last_compass_sent[(officer_a.guid, criminal.guid)] = now - 25
        _last_compass_sent[(officer_b.guid, criminal.guid)] = now - 25
        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_not_called()

        # 35 s since last send: past the effective 30 s → both fire again
        now = time.monotonic()
        _last_compass_sent[(officer_a.guid, criminal.guid)] = now - 35
        _last_compass_sent[(officer_b.guid, criminal.guid)] = now - 35
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        self.assertEqual(mock_sys_msg.await_count, 2)

    async def test_force_budget_capped_at_two(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Four receiving officers at 1 km: effective interval is solo × 2
        (30 s), NOT solo × 4 — a large response is never slower per cop
        than a pair (freeman, 2026-09-20)."""
        criminal = await self._setup_criminal()
        officers = [await self._setup_police() for _ in range(4)]

        mock_get_players.return_value = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC)]
            + [
                _make_player_data(o.player.unique_id, o.guid, *_COMPASS_COP_1KM)
                for o in officers
            ]
        )
        mock_get_locations.return_value = []
        mock_police.return_value = _AsyncList(officers)
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        # First tick — all four receive (no prior state)
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        self.assertEqual(mock_sys_msg.await_count, 4)

        # 35 s since last send: past the capped 2 × 15 = 30 s → all fire
        # again. With the old uncapped ×4 budget (60 s) all four would stay
        # silent here.
        now = time.monotonic()
        for o in officers:
            _last_compass_sent[(o.guid, criminal.guid)] = now - 35
        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        self.assertEqual(mock_sys_msg.await_count, 4)

    async def test_ring_cop_excluded_from_budget(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """A cop inside the 200 m ring gets the <200m ping but doesn't count
        into the budget: the 1 km cop's effective interval stays solo × 1
        (15 s), not ×2 (30 s)."""
        criminal = await self._setup_criminal()
        officer_near = await self._setup_police()
        officer_far = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer_near.player.unique_id, officer_near.guid, *_COMPASS_COP_CLOSE),
            _make_player_data(officer_far.player.unique_id, officer_far.guid, *_COMPASS_COP_1KM),
        ])
        mock_get_locations.return_value = []
        mock_police.return_value = _AsyncList([officer_near, officer_far])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        # First tick — both receive (close ping + bearing)
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        self.assertEqual(mock_sys_msg.await_count, 2)

        # t-10 for the ring cop (within its fixed 15 s → silent) and t-20
        # for the far cop: past solo 15 s (N=1) → receives. With a
        # wrongly-counted ring cop (N=2 → 30 s) the far cop would stay
        # silent here.
        now = time.monotonic()
        _last_compass_sent[(officer_near.guid, criminal.guid)] = now - 10
        _last_compass_sent[(officer_far.guid, criminal.guid)] = now - 20
        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_awaited_once()
        self.assertEqual(
            mock_sys_msg.await_args.kwargs.get("character_guid"), officer_far.guid
        )

    async def test_two_officers_both_far_both_receive(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Two officers beyond 500 m → both receive compass messages."""
        criminal = await self._setup_criminal()
        officer_a = await self._setup_police()
        officer_b = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer_a.player.unique_id, officer_a.guid, *_COMPASS_COP_1KM),
            _make_player_data(officer_b.player.unique_id, officer_b.guid, *_COMPASS_COP_3KM),
        ])
        mock_get_locations.return_value = []
        mock_police.return_value = _AsyncList([officer_a, officer_b])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        self.assertEqual(mock_sys_msg.await_count, 2)
        received = {c.kwargs.get("character_guid") for c in mock_sys_msg.await_args_list}
        self.assertEqual(received, {officer_a.guid, officer_b.guid})
        # Pair keys recorded for both
        self.assertIn((officer_a.guid, criminal.guid), _last_compass_sent)
        self.assertIn((officer_b.guid, criminal.guid), _last_compass_sent)

    async def test_mgmt_api_unavailable_degrades_to_stationary_cadence(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """API unavailable → stationary cadence (1 km: 15 s), NOT unthrottled."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_1KM),
        ])
        mock_get_locations.return_value = None
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        # First tick — sends
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_awaited_once()

        # 10 s since last send → still within the stationary interval
        _last_compass_sent[(officer.guid, criminal.guid)] = time.monotonic() - 10
        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_not_called()

        # 20 s since last send → past the stationary interval → sends
        _last_compass_sent[(officer.guid, criminal.guid)] = time.monotonic() - 20
        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_awaited_once()

    async def test_lowercase_guid_matches_uppercase_speed_map(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Character GUID in DB lowercase, speed map uppercase → matched."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_3KM),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 5556),  # uppercase in speed map
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_awaited_once()

    async def test_stale_pair_keys_purged_when_suspect_no_longer_wanted(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Pair keys whose suspect is no longer wanted are purged."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        _last_compass_sent[(officer.guid, "gone-suspect")] = 999.0
        _last_compass_sent[(officer.guid, criminal.guid)] = 999.0

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_1KM),
        ])
        mock_get_locations.return_value = []
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        self.assertNotIn((officer.guid, "gone-suspect"), _last_compass_sent)
        self.assertIn((officer.guid, criminal.guid), _last_compass_sent)

    async def test_keys_for_offline_officers_purged(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Pair keys belonging to officers no longer on duty are purged."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        _last_compass_sent[("offline-cop", criminal.guid)] = 999.0

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_1KM),
        ])
        mock_get_locations.return_value = []
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        self.assertNotIn(("offline-cop", criminal.guid), _last_compass_sent)

    # -------------------------------------------------------------------
    # Message content and robustness
    # -------------------------------------------------------------------

    async def test_officer_and_suspect_same_guid_skipped(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """If an officer has the same GUID as a suspect, that entry is skipped."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            # Officer shares the suspect's guid → same location → silent
            _make_player_data(officer.player.unique_id, criminal.guid, *_COMPASS_COP_1KM),
        ])
        mock_get_locations.return_value = []
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        # Officer entry resolves to the suspect location (500 m away → 1 km cop
        # coordinates unused); officer's own pair is skipped by the guid guard
        mock_sys_msg.assert_not_called()

    async def test_message_contains_distance_and_direction(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Compass message includes distance and cardinal direction."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_1KM),
        ])
        mock_get_locations.return_value = []
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_awaited_once()
        message = mock_sys_msg.await_args.args[1]
        self.assertIn(criminal.name, message)
        self.assertIn("1.0km", message)
        self.assertIn("270°W", message)  # suspect is due west of the officer

    async def test_two_suspects_independent_pair_throttles(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Two suspects: one within its interval, one past → only the stale
        one appears in the officer's message."""
        criminal_a = await self._setup_criminal()
        criminal_b = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal_a.player.unique_id, criminal_a.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(criminal_b.player.unique_id, criminal_b.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_1KM),
        ])
        mock_get_locations.return_value = []
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        # Seed A as recently sent (within the 15 s ceiling), leave B unseeded.
        _last_compass_sent[(officer.guid, criminal_a.guid)] = time.monotonic() - 10

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_awaited_once()
        message = mock_sys_msg.await_args.args[1]
        self.assertNotIn(criminal_a.name, message)
        self.assertIn(criminal_b.name, message)

    async def test_send_failure_does_not_crash(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """send_system_message raising does not crash the tick."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_1KM),
        ])
        mock_get_locations.return_value = []
        mock_police.return_value = _AsyncList([officer])
        mock_sys_msg.side_effect = Exception("send failed")
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        # Must not raise
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
