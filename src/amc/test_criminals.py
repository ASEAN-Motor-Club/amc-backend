"""Tests for the wanted countdown tick (amc.criminals).

Hybrid mechanic:
  - Online suspects always decay at BASE_DECAY_PER_TICK (1.0/tick).
    Clears in BASE_WANTED_DURATION (e.g. 900 s = 15 min) with no police.
  - Police proximity SLOWS decay via 1/r² law:
      effective_decay = BASE_DECAY_PER_TICK / (1 + proximity_factor)
    Closer police → larger factor → slower decay. Decay never reverses.
  - Escape gate: cannot expire while any cop is within ESCAPE_DISTANCE (500m).
    Clamped at ESCAPE_FLOOR. Clears freely once all cops are beyond 500m.
  - Offline suspects: no decay, wanted persists indefinitely.
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
    BASE_WANTED_DURATION,
    CRIMINAL_RECORD_DECAY_FACTOR,
    CRIMINAL_SUSPECT_DURATION,
    ESCAPE_DISTANCE,
    ESCAPE_FLOOR,
    ESCAPE_MESSAGE,
    TICK_INTERVAL,
    _compute_stars,
    _costume_reconciled_guids,
    _last_compass_sent,
    _last_escape_msg_sent,
    _last_star_notified,
    _last_suspect_guids,
    refresh_suspect_tags,
    tick_criminal_record_decay,
    tick_police_suspect_locations,
    tick_wanted_countdown,
)
from amc.factories import CharacterFactory, PlayerFactory
from amc.models import CriminalRecord, PoliceSession, Wanted


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
_COP_CLOSE    = (5000 + 1000, 5000, 0)    # 1000 units = 10m  (clamped to MIN_DISTANCE=50m → factor=10)
_COP_MED      = (5000 + 10_000, 5000, 0)  # 10_000 units = 100m (factor=4.0 with REF_DISTANCE=200m)
_COP_REF      = (5000 + Wanted.REF_DISTANCE, 5000, 0)  # at REF_DISTANCE → factor=1.0 → decay = BASE/2
_COP_ESCAPED  = (5000 + ESCAPE_DISTANCE + 1000, 5000, 0)  # > ESCAPE_DISTANCE away
_COP_FAR      = (5000 + 100_000, 5000, 0)  # 1000m — well beyond escape distance


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
        """ESCAPE_FLOOR (0.1) is above 0 so still counts as 1 star."""
        self.assertEqual(_compute_stars(ESCAPE_FLOOR), 1)


@patch("amc.criminals.refresh_player_name", new_callable=AsyncMock)
@patch("amc.criminals.send_system_message", new_callable=AsyncMock)
class WantedCountdownTickTests(TestCase):
    """Integration tests for tick_wanted_countdown."""

    def setUp(self):
        _last_star_notified.clear()
        _last_escape_msg_sent.clear()
        _last_suspect_guids.clear()

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
    # Base time-based decay — always ticks without police
    # -----------------------------------------------------------------------

    async def test_no_cops_online_decays_at_base_rate(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Online suspect decays at BASE_DECAY_PER_TICK even with no police online."""
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
        # 10 ticks × 1.0/tick = 10 units decay → 290
        self.assertAlmostEqual(wanted.wanted_remaining, 300 - 10 * BASE_DECAY_PER_TICK, delta=0.1)
        self.assertIsNone(wanted.expired_at)

    async def test_no_cops_online_expires_after_base_duration(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Wanted expires after BASE_WANTED_DURATION ticks with no police."""
        criminal = await self._setup_criminal(wanted_remaining=BASE_WANTED_DURATION)
        players = _make_players_list(
            [_make_player_data(criminal.player.unique_id, criminal.guid, *_SUSPECT_LOC)]
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(BASE_WANTED_DURATION):
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
    # Police proximity — slows decay (1/r²)
    # -----------------------------------------------------------------------

    async def test_close_cop_decays_slower_than_no_police(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Close cop slows decay: wanted_remaining is higher with cop than without."""
        # Criminal A: cop nearby (slows decay)
        criminal_a = await self._setup_criminal(wanted_remaining=200)
        officer = await self._setup_police()

        # Criminal B: no cop (full decay rate)
        criminal_b = await self._setup_criminal(wanted_remaining=200)

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_CLOSE  # 10m → factor=10 → effective_decay = 1.0/(1+10) ≈ 0.09/tick

        # Scenario A: cop at 10m slows decay
        players_a = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal_a.player.unique_id, criminal_a.guid, sx, sy, sz),
        ])
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players_a):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)
        wanted_a = await Wanted.objects.aget(character=criminal_a)

        # Scenario B: no cops — full decay
        players_b = _make_players_list([
            _make_player_data(criminal_b.player.unique_id, criminal_b.guid, sx, sy, sz),
        ])
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players_b):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)
        wanted_b = await Wanted.objects.aget(character=criminal_b)

        # Cop nearby → slower decay → higher remaining
        self.assertGreater(wanted_a.wanted_remaining, wanted_b.wanted_remaining)

    async def test_close_cop_decays_slower_than_medium_cop(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Closer cop = more slowing: 10m decays slower than 100m."""
        criminal_close = await self._setup_criminal(wanted_remaining=200)
        officer_close = await self._setup_police()

        criminal_med = await self._setup_criminal(wanted_remaining=200)
        officer_med = await self._setup_police()

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        sx, sy, sz = _SUSPECT_LOC

        # Close cop (10m): factor=10, effective_decay ≈ 0.09/tick
        players_close = _make_players_list([
            _make_player_data(officer_close.player.unique_id, officer_close.guid, *_COP_CLOSE),
            _make_player_data(criminal_close.player.unique_id, criminal_close.guid, sx, sy, sz),
        ])
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players_close):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)
        wanted_close = await Wanted.objects.aget(character=criminal_close)

        # Medium cop (100m): factor=1, effective_decay = 0.5/tick
        players_med = _make_players_list([
            _make_player_data(officer_med.player.unique_id, officer_med.guid, *_COP_MED),
            _make_player_data(criminal_med.player.unique_id, criminal_med.guid, sx, sy, sz),
        ])
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players_med):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)
        wanted_med = await Wanted.objects.aget(character=criminal_med)

        # Closer cop → slower decay → higher remaining
        self.assertGreater(wanted_close.wanted_remaining, wanted_med.wanted_remaining)

    async def test_med_cop_decays_at_half_base_rate(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Cop at REF_DISTANCE: factor=1.0, effective_decay = BASE_DECAY_PER_TICK / 2."""
        criminal = await self._setup_criminal(wanted_remaining=200)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_REF  # at REF_DISTANCE → factor=1.0 → decay = BASE_DECAY_PER_TICK / 2
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
        # 10 ticks × (BASE_DECAY_PER_TICK / 2) × TICK_INTERVAL
        expected = 200 - 10 * (BASE_DECAY_PER_TICK / 2.0) * TICK_INTERVAL
        self.assertAlmostEqual(wanted.wanted_remaining, expected, delta=0.5)
        self.assertIsNone(wanted.expired_at)

    async def test_cop_beyond_escape_distance_uses_full_rate(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Cop beyond ESCAPE_DISTANCE: no proximity effect, full BASE_DECAY_PER_TICK applies."""
        criminal = await self._setup_criminal(wanted_remaining=200)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_ESCAPED  # > 200m
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
        # Full decay: 10 × BASE_DECAY_PER_TICK × TICK_INTERVAL
        expected = 200 - 10 * BASE_DECAY_PER_TICK * TICK_INTERVAL
        self.assertAlmostEqual(wanted.wanted_remaining, expected, delta=0.5)
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
    # Escape gate — cannot expire while near police
    # -----------------------------------------------------------------------

    async def test_near_cop_clamps_at_escape_floor(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Wanted cannot drop below ESCAPE_FLOOR while police are within ESCAPE_DISTANCE."""
        criminal = await self._setup_criminal(wanted_remaining=1.0)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_MED  # within 200m
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(20):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertGreaterEqual(wanted.wanted_remaining, ESCAPE_FLOOR)
        self.assertIsNone(wanted.expired_at)

    async def test_cannot_expire_near_police(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Wanted at ESCAPE_FLOOR near police never expires."""
        criminal = await self._setup_criminal(wanted_remaining=ESCAPE_FLOOR)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_MED
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(20):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, ESCAPE_FLOOR)
        self.assertIsNone(wanted.expired_at)

    async def test_beyond_escape_distance_expires_normally(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Suspect beyond ESCAPE_DISTANCE decays at full rate and can expire."""
        criminal = await self._setup_criminal(wanted_remaining=5.0)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        ex, ey, ez = _COP_ESCAPED  # > 200m
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, ex, ey, ez),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(10):  # 10 × 1.0/tick = 10 > 5 → expires
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, 0)
        self.assertIsNotNone(wanted.expired_at)

    async def test_full_lifecycle_near_then_escape(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Full lifecycle: runs down near police (slowed) then expires quickly once beyond 200m."""
        # With INITIAL=300, cop at 100m → 0.5/tick, so from 300 needs 600 ticks to clear
        # Start low to keep test fast
        criminal = await self._setup_criminal(wanted_remaining=2.0)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_MED  # 100m → 0.5/tick → 4 ticks to drain 2.0 to floor

        near_players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        # Phase 1: near police — slows down, floors at ESCAPE_FLOOR
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=near_players):
            for _ in range(10):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, ESCAPE_FLOOR)
        self.assertIsNone(wanted.expired_at)

        # Phase 2: escape beyond ESCAPE_DISTANCE → full rate, clears in 1 tick
        ex, ey, ez = _COP_ESCAPED
        escaped_players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, ex, ey, ez),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=escaped_players):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        wanted = await Wanted.objects.aget(character=criminal)
        self.assertEqual(wanted.wanted_remaining, 0)
        self.assertIsNotNone(wanted.expired_at)

    # -----------------------------------------------------------------------
    # Escape popup messages
    # -----------------------------------------------------------------------

    async def test_escape_popup_sent_when_at_floor_near_police(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Escape popup is sent when suspect heat is clamped at ESCAPE_FLOOR near police."""
        criminal = await self._setup_criminal(wanted_remaining=ESCAPE_FLOOR)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, *_COP_MED),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            await tick_wanted_countdown(mock_http, mock_http_mod)

        escape_calls = [
            c for c in mock_sys_msg.call_args_list
            if len(c.args) > 1 and c.args[1] == ESCAPE_MESSAGE
        ]
        self.assertEqual(len(escape_calls), 1)
        self.assertEqual(escape_calls[0].kwargs["character_guid"], criminal.guid)

    async def test_escape_popup_throttled(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Escape popup is not sent again within ESCAPE_MSG_COOLDOWN seconds."""
        criminal = await self._setup_criminal(wanted_remaining=ESCAPE_FLOOR)
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

        escape_calls = [
            c for c in mock_sys_msg.call_args_list
            if len(c.args) > 1 and c.args[1] == ESCAPE_MESSAGE
        ]
        self.assertEqual(len(escape_calls), 1)

    async def test_escape_popup_not_sent_when_above_floor(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """Escape popup is not sent while wanted_remaining is well above floor."""
        criminal = await self._setup_criminal(wanted_remaining=300)
        officer = await self._setup_police()

        sx, sy, sz = _SUSPECT_LOC
        cx, cy, cz = _COP_MED
        players = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, cx, cy, cz),
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, sz),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            for _ in range(3):
                await tick_wanted_countdown(mock_http, mock_http_mod)

        escape_calls = [
            c for c in mock_sys_msg.call_args_list
            if len(c.args) > 1 and c.args[1] == ESCAPE_MESSAGE
        ]
        self.assertEqual(len(escape_calls), 0)

    async def test_escape_msg_state_cleaned_on_expiry(
        self,
        mock_sys_msg,
        mock_refresh,
    ):
        """_last_escape_msg_sent is cleaned up when wanted expires."""
        criminal = await self._setup_criminal(wanted_remaining=0.5)
        officer = await self._setup_police()

        _last_escape_msg_sent[criminal.guid] = time.monotonic()

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

        self.assertNotIn(criminal.guid, _last_escape_msg_sent)

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

        star_calls = [
            c for c in mock_sys_msg.call_args_list
            if len(c.args) > 1 and c.args[1] != ESCAPE_MESSAGE
        ]
        self.assertEqual(len(star_calls), 0)

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
        self.assertIn("no longer wanted", mock_announce.call_args.args[0])
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
        sx, sy, sz = _SUSPECT_LOC
        underwater_z = UNDERWATER_Z_THRESHOLD - 1
        players = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, underwater_z),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            with patch("amc.criminals.execute_arrest", new_callable=AsyncMock, return_value=([criminal.name], 1000)) as mock_arrest:
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
        sx, sy, sz = _SUSPECT_LOC
        players = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, UNDERWATER_Z_THRESHOLD),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            with patch("amc.criminals.execute_arrest", new_callable=AsyncMock) as mock_arrest:
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
        sx, sy, sz = _SUSPECT_LOC
        players = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, sx, sy, UNDERWATER_Z_THRESHOLD + 1),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            with patch("amc.criminals.execute_arrest", new_callable=AsyncMock) as mock_arrest:
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
        sx, sy, sz = _SUSPECT_LOC
        underwater_z = UNDERWATER_Z_THRESHOLD - 1
        players = _make_players_list([
            _make_player_data(criminal_a.player.unique_id, criminal_a.guid, sx, sy, underwater_z),
            _make_player_data(criminal_b.player.unique_id, criminal_b.guid, 6000, 6000, 0),
        ])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players):
            with patch("amc.criminals.execute_arrest", new_callable=AsyncMock, side_effect=ValueError("Jail not configured")) as mock_arrest:
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


@patch("amc.criminals.make_suspect", new_callable=AsyncMock)
class RefreshSuspectTagsTests(TestCase):
    """Tests for refresh_suspect_tags — decoupled from tick_wanted_countdown."""

    def setUp(self):
        _last_suspect_guids.clear()
        _costume_reconciled_guids.clear()

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
        await CriminalRecord.objects.acreate(character=character, reason="Test", confiscatable_amount=1000)
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
        await CriminalRecord.objects.acreate(character=character, reason="Test", confiscatable_amount=1000)
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
        await CriminalRecord.objects.acreate(
            character=character, reason="Test", confiscatable_amount=1000,
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
        await CriminalRecord.objects.acreate(
            character=character, reason="Test", confiscatable_amount=1000,
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
        await CriminalRecord.objects.acreate(character=character, reason="Test", confiscatable_amount=1000)

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

    async def test_within_100m_proximity_hides_suspect(
        self,
    ):
        """Suspect within 100m of an officer is hidden by the 500m proximity filter."""
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

        # Hidden by proximity — no message
        mock_sys_msg.assert_not_called()

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
        # Close suspect hidden by proximity
        self.assertNotIn(criminal_close.name, message)
        # Far suspect shown
        self.assertIn(criminal_far.name, message)
        self.assertIn("600m", message)


@patch("amc.criminals.make_suspect", new_callable=AsyncMock)
class CriminalRecordDecayTests(TestCase):
    """Tests for tick_criminal_record_decay."""

    def setUp(self):
        _last_suspect_guids.clear()
        _costume_reconciled_guids.clear()

    async def _setup_criminal_record(self, confiscatable_amount=10000):
        """Create a character with an active criminal record."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
        )
        await character.asave(update_fields=["last_online"])
        record = await CriminalRecord.objects.acreate(
            character=character,
            reason="Test",
            confiscatable_amount=confiscatable_amount,
        )
        return record

    async def test_afk_player_does_not_decay(
        self,
        mock_make_suspect,
    ):
        """AFK players are excluded from criminal record decay."""
        record = await self._setup_criminal_record(confiscatable_amount=10000)
        mock_http_mod = AsyncMock()

        with patch("amc.mod_server.get_player", new_callable=AsyncMock, return_value={"bAFK": True}):
            await tick_criminal_record_decay(mock_http_mod)

        record = await CriminalRecord.objects.aget(pk=record.pk)
        self.assertEqual(record.confiscatable_amount, 10000)

    async def test_non_afk_player_decays(
        self,
        mock_make_suspect,
    ):
        """Non-AFK online players have their confiscatable amount decayed."""
        record = await self._setup_criminal_record(confiscatable_amount=10000)
        mock_http_mod = AsyncMock()

        with patch("amc.mod_server.get_player", new_callable=AsyncMock, return_value={"bAFK": False}), \
             patch("amc.mod_server.get_player_last_vehicle", new_callable=AsyncMock, return_value={"vehicle": {"id": 1}}), \
             patch("amc.mod_server.get_player_last_vehicle_parts", new_callable=AsyncMock, return_value={"parts": []}), \
             patch("amc.mod_detection.detect_custom_parts", return_value=[]):
            await tick_criminal_record_decay(mock_http_mod)

        record = await CriminalRecord.objects.aget(pk=record.pk)
        expected = int(10000 * CRIMINAL_RECORD_DECAY_FACTOR)
        self.assertEqual(record.confiscatable_amount, expected)

    async def test_offline_player_does_not_decay(
        self,
        mock_make_suspect,
    ):
        """Offline players are not included in the decay at all."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now() - timedelta(minutes=5),
        )
        await character.asave(update_fields=["last_online"])
        record = await CriminalRecord.objects.acreate(
            character=character,
            reason="Test",
            confiscatable_amount=10000,
        )
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.make_suspect", new_callable=AsyncMock):
            await tick_criminal_record_decay(mock_http_mod)

        record = await CriminalRecord.objects.aget(pk=record.pk)
        self.assertEqual(record.confiscatable_amount, 10000)

    async def test_modded_vehicle_player_does_not_decay(
        self,
        mock_make_suspect,
    ):
        """Players in modded vehicles are excluded from decay."""
        record = await self._setup_criminal_record(confiscatable_amount=10000)
        mock_http_mod = AsyncMock()

        with patch("amc.mod_server.get_player", new_callable=AsyncMock, return_value={"bAFK": False}), \
             patch("amc.mod_server.get_player_last_vehicle", new_callable=AsyncMock, return_value={"vehicle": {"id": 1}}), \
             patch("amc.mod_server.get_player_last_vehicle_parts", new_callable=AsyncMock, return_value={"parts": [{"Key": "mod_part", "Slot": 0}]}), \
             patch("amc.mod_detection.detect_custom_parts", return_value=[{"key": "mod_part"}]):
            await tick_criminal_record_decay(mock_http_mod)

        record = await CriminalRecord.objects.aget(pk=record.pk)
        self.assertEqual(record.confiscatable_amount, 10000)

    async def test_modded_vehicle_closes_record_at_zero(
        self,
        mock_make_suspect,
    ):
        """Modded-vehicle players still have records closed when already below floor.

        Regression test: previously the modded-vehicle ``continue`` skipped
        both decay AND closure, so a record that had already decayed to 0
        (or was created at 0 via a costume equip) would stay open forever
        as long as the player was in a vehicle with any custom parts.
        """
        record = await self._setup_criminal_record(confiscatable_amount=0)
        mock_http_mod = AsyncMock()

        with patch("amc.mod_server.get_player", new_callable=AsyncMock, return_value={"bAFK": False}), \
             patch("amc.mod_server.get_player_last_vehicle", new_callable=AsyncMock, return_value={"vehicle": {"id": 1}}), \
             patch("amc.mod_server.get_player_last_vehicle_parts", new_callable=AsyncMock, return_value={"parts": [{"Key": "mod_part", "Slot": 0}]}), \
             patch("amc.mod_detection.detect_custom_parts", return_value=[{"key": "mod_part"}]):
            await tick_criminal_record_decay(mock_http_mod)

        record = await CriminalRecord.objects.aget(pk=record.pk)
        self.assertEqual(record.confiscatable_amount, 0)
        self.assertIsNotNone(record.cleared_at)

    async def test_modded_costume_wearer_record_stays_open_at_zero(
        self,
        mock_make_suspect,
    ):
        """Costume wearers in modded vehicles keep the record open even at 0."""
        record = await self._setup_criminal_record(confiscatable_amount=0)
        record.character.wearing_costume = True
        await record.character.asave(update_fields=["wearing_costume"])
        mock_http_mod = AsyncMock()

        with patch("amc.mod_server.get_player", new_callable=AsyncMock, return_value={"bAFK": False}), \
             patch("amc.mod_server.get_player_last_vehicle", new_callable=AsyncMock, return_value={"vehicle": {"id": 1}}), \
             patch("amc.mod_server.get_player_last_vehicle_parts", new_callable=AsyncMock, return_value={"parts": [{"Key": "mod_part", "Slot": 0}]}), \
             patch("amc.mod_detection.detect_custom_parts", return_value=[{"key": "mod_part"}]):
            await tick_criminal_record_decay(mock_http_mod)

        record = await CriminalRecord.objects.aget(pk=record.pk)
        self.assertEqual(record.confiscatable_amount, 0)
        self.assertIsNone(record.cleared_at)

    async def test_applies_suspect_to_online_criminals_via_refresh_suspect_tags(
        self,
        mock_make_suspect,
    ):
        """Online active criminals wearing a costume get make_suspect called via refresh_suspect_tags."""
        _costume_reconciled_guids.clear()
        record = await self._setup_criminal_record(confiscatable_amount=10000)
        record.character.wearing_costume = True
        record.character.costume_item_key = "Costume_Police_01"
        await record.character.asave(update_fields=["wearing_costume", "costume_item_key"])

        players = _make_players_list(
            [_make_player_data(record.character.player.unique_id, record.character.guid, *_SUSPECT_LOC)]
        )
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=players), \
             patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=None):
            await refresh_suspect_tags(mock_http_mod)

        mock_make_suspect.assert_any_call(
            mock_http_mod, record.character.guid, duration_seconds=CRIMINAL_SUSPECT_DURATION
        )

    async def test_skips_suspect_for_offline_criminals_via_refresh_suspect_tags(
        self,
        mock_make_suspect,
    ):
        """Offline criminals are not made suspect via refresh_suspect_tags."""
        _costume_reconciled_guids.clear()
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now() - timedelta(minutes=5),
            wearing_costume=True,
            costume_item_key="Costume_Police_01",
        )
        await character.asave(update_fields=["last_online", "wearing_costume", "costume_item_key"])
        await CriminalRecord.objects.acreate(
            character=character,
            reason="Test",
            confiscatable_amount=10000,
        )
        mock_http_mod = AsyncMock()

        with patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=[]), \
             patch("amc.criminals.get_player_customization", new_callable=AsyncMock, return_value=None):
            await refresh_suspect_tags(mock_http_mod)

        costume_calls = [
            c for c in mock_make_suspect.call_args_list
            if c.args[1] == character.guid and c.kwargs.get("duration_seconds") == CRIMINAL_SUSPECT_DURATION
        ]
        self.assertEqual(len(costume_calls), 0)

    async def test_skips_suspect_for_criminals_without_guid(
        self,
        mock_make_suspect,
    ):
        """Criminals without a guid are not made suspect."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            guid=None,
            last_online=timezone.now(),
        )
        await character.asave(update_fields=["last_online"])
        await CriminalRecord.objects.acreate(
            character=character,
            reason="Test",
            confiscatable_amount=10000,
        )
        mock_http_mod = AsyncMock()

        await tick_criminal_record_decay(mock_http_mod)

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
        _last_escape_msg_sent.clear()
        _last_suspect_guids.clear()
        _costume_reconciled_guids.clear()

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
             patch("amc.criminals.announce", new_callable=AsyncMock), \
             patch("amc.criminals.announce_money_secured", new_callable=AsyncMock):
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
             patch("amc.criminals.announce", new_callable=AsyncMock), \
             patch("amc.criminals.announce_money_secured", new_callable=AsyncMock):
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
        await CriminalRecord.objects.acreate(
            character=character, reason="Test", confiscatable_amount=1000,
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
        await CriminalRecord.objects.acreate(
            character=character, reason="Test", confiscatable_amount=1000,
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
# Officer at origin — well beyond 500m proximity hide
_COMPASS_COP_FAR = (0, 0, 0)
# Officer very close to suspect — within 500m proximity hide
_COMPASS_COP_CLOSE = (50000 + 1000, 50000, 0)  # 10m away


@patch("amc.criminals.send_system_message", new_callable=AsyncMock)
@patch("amc.police.get_active_police_characters", new_callable=AsyncMock)
@patch("amc.criminals.get_players_locations", new_callable=AsyncMock)
@patch("amc.criminals.get_players", new_callable=AsyncMock)
class CompassTickTests(TestCase):
    """Exhaustive tests for tick_police_suspect_locations speed-based
    throttle and proximity filtering."""

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
        _last_compass_sent["some-guid"] = 999.0
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

        mock_get_players.return_value = _make_players_list([
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(officer.guid, 0),
        ]
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
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 3000),
        ]
        mock_police.return_value = _AsyncList([])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_not_called()

    # -------------------------------------------------------------------
    # Speed-based throttle — stationary / walking cutoff
    # -------------------------------------------------------------------

    async def test_walking_speed_suspect_no_compass(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Walking speed (< 5 m/s = 500 game units/s) → no compass update."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        # 300 game units/s = 3 m/s → below 5 m/s cutoff
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 300),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_not_called()

    async def test_exact_cutoff_speed_no_compass(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Exactly at cutoff (499 game units/s = 4.99 m/s) → no compass update."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 499),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_not_called()

    async def test_zero_speed_suspect_no_compass(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Zero speed (stationary) → no compass update."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 0),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_not_called()

    # -------------------------------------------------------------------
    # Speed-based throttle — vehicle speed sends compass
    # -------------------------------------------------------------------

    async def test_driving_speed_sends_compass(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Driving speed (3000 game units/s = 30 m/s) → compass sent."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 3000),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_awaited_once()
        msg = mock_sys_msg.call_args.args[1]
        self.assertIn(criminal.name, msg)

    async def test_minimum_vehicle_speed_sends_compass(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Just above cutoff (500 game units/s = 5 m/s) → compass sent."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 500),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_awaited_once()

    async def test_fast_driving_sends_compass(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Fast driving (6000 game units/s = 60 m/s) → compass sent."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 6000),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_awaited_once()

    # -------------------------------------------------------------------
    # Throttle interval — not yet elapsed → skipped
    # -------------------------------------------------------------------

    async def test_interval_not_elapsed_skips_suspect(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Suspect sent recently (within interval) → skipped on next tick."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        # 3000 game units/s = 30 m/s → interval = 300/30 = 10s
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 3000),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        # First tick — sends compass
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        self.assertEqual(mock_sys_msg.await_count, 1)

        # Second tick immediately (0s elapsed) — should NOT send again
        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_not_called()

    async def test_interval_elapsed_sends_again(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Suspect sent > interval ago → sent again on next tick."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        # 3000 game units/s = 30 m/s → interval = 10s
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 3000),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        # First tick — sends compass
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        self.assertEqual(mock_sys_msg.await_count, 1)

        # Simulate interval elapsed by backdating _last_compass_sent
        _last_compass_sent[criminal.guid] = time.monotonic() - 11  # 11s ago > 10s interval

        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_awaited_once()

    async def test_interval_boundary_not_yet_elapsed(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Just before interval boundary → not sent."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        # 3000 game units/s = 30 m/s → interval = 10s
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 3000),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        # First tick
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        # Backdate to 9.9s ago — just under 10s interval
        _last_compass_sent[criminal.guid] = time.monotonic() - 9.9

        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_not_called()

    # -------------------------------------------------------------------
    # Interval clamping — 5s min, 60s max
    # -------------------------------------------------------------------

    async def test_fast_speed_clamped_to_5s_interval(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Very fast speed → interval clamped to 5s minimum."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        # 60000 game units/s = 600 m/s → interval = 300/600 = 0.5 → clamped to 5s
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 60000),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        # First tick
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        # Backdate to 4.9s → not yet 5s
        _last_compass_sent[criminal.guid] = time.monotonic() - 4.9
        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_not_called()

        # Backdate to 5.1s → past 5s
        _last_compass_sent[criminal.guid] = time.monotonic() - 5.1
        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_awaited_once()

    async def test_slow_speed_clamped_to_60s_interval(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Slow vehicle speed → interval clamped to 60s maximum."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        # 500 game units/s = 5 m/s → interval = 300/5 = 60s (at max clamp)
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 500),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        # First tick
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        # Backdate to 59s → not yet 60s
        _last_compass_sent[criminal.guid] = time.monotonic() - 59
        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_not_called()

        # Backdate to 61s → past 60s
        _last_compass_sent[criminal.guid] = time.monotonic() - 61
        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_awaited_once()

    # -------------------------------------------------------------------
    # Mod management API unavailable — graceful degradation
    # -------------------------------------------------------------------

    async def test_mgmt_api_unavailable_sends_all_unthrottled(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """get_players_locations returns None → all suspects sent unthrottled."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        mock_get_locations.return_value = None
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        # Walking suspect would be throttled normally, but API unavailable → sent
        mock_sys_msg.assert_awaited_once()
        self.assertIn(criminal.guid, _last_compass_sent)

    async def test_mgmt_api_unavailable_sends_walking_suspects(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """API unavailable → walking-speed suspects also sent (no throttle)."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        mock_get_locations.return_value = None
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_awaited_once()

    async def test_mgmt_api_unavailable_sends_repeatedly(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """API unavailable → suspects sent every tick (no throttle)."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        mock_get_locations.return_value = None
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        self.assertEqual(mock_sys_msg.await_count, 1)

        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        self.assertEqual(mock_sys_msg.await_count, 1)

    # -------------------------------------------------------------------
    # Proximity filter — 500m hide
    # -------------------------------------------------------------------

    async def test_officer_within_500m_hides_suspect_from_all(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Officer within 500m of suspect → hidden from ALL officers."""
        criminal = await self._setup_criminal()
        officer_near = await self._setup_police()
        officer_far = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer_near.player.unique_id, officer_near.guid, *_COMPASS_COP_CLOSE),
            _make_player_data(officer_far.player.unique_id, officer_far.guid, *_COMPASS_COP_FAR),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 3000),
        ]
        mock_police.return_value = _AsyncList([officer_near, officer_far])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_not_called()
        self.assertNotIn(criminal.guid, _last_compass_sent)

    async def test_officer_beyond_500m_shows_suspect(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """All officers beyond 500m → suspect visible."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 3000),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_awaited_once()
        self.assertIn(criminal.guid, _last_compass_sent)

    async def test_mixed_proximity_some_near_some_far(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """One suspect near officer, one far → only far suspect sent."""
        criminal_near = await self._setup_criminal()
        criminal_far = await self._setup_criminal()
        officer = await self._setup_police()

        # officer at origin; near suspect within 500m (200m), far suspect beyond (2km)
        near_loc = (20000, 0, 0)  # 200m from officer — within 500m proximity
        far_loc = (200000, 0, 0)  # 2km from officer — beyond proximity

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal_near.player.unique_id, criminal_near.guid, *near_loc),
            _make_player_data(criminal_far.player.unique_id, criminal_far.guid, *far_loc),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal_near.guid, 3000),
            _make_mgmt_entry(criminal_far.guid, 3000),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        msg = mock_sys_msg.call_args.args[1]
        self.assertIn(criminal_far.name, msg)
        self.assertNotIn(criminal_near.name, msg)
        self.assertNotIn(criminal_near.guid, _last_compass_sent)
        self.assertIn(criminal_far.guid, _last_compass_sent)

    async def test_proximity_does_not_consume_interval_slot(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Suspect filtered by proximity → interval NOT consumed →
        appears immediately when officer moves away."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        # Phase 1: officer close → suspect hidden
        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_CLOSE),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 3000),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        mock_sys_msg.assert_not_called()
        self.assertNotIn(criminal.guid, _last_compass_sent)

        # Phase 2: officer moves away → suspect should appear IMMEDIATELY
        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_awaited_once()
        msg = mock_sys_msg.call_args.args[1]
        self.assertIn(criminal.name, msg)

    # -------------------------------------------------------------------
    # GUID normalization — uppercase lookup
    # -------------------------------------------------------------------

    async def test_lowercase_guid_in_wanted_matches_uppercase_speed_map(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Character GUID in DB is lowercase, speed map has uppercase → match."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        lower_guid = criminal.guid.lower()
        criminal.guid = lower_guid
        await criminal.asave(update_fields=["guid"])

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, lower_guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(lower_guid, 3000),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_awaited_once()
        msg = mock_sys_msg.call_args.args[1]
        self.assertIn(criminal.name, msg)

    async def test_guid_not_in_speed_map_treated_as_zero_speed(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Suspect GUID missing from speed_map → treated as 0 speed → no compass."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        # Speed map has entries but NOT for this suspect
        mock_get_locations.return_value = [
            _make_mgmt_entry("some-other-guid", 3000),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_not_called()

    # -------------------------------------------------------------------
    # Stale GUID cleanup
    # -------------------------------------------------------------------

    async def test_stale_compass_state_cleaned_when_no_wanted(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """_last_compass_sent cleared when no wanted records exist."""
        _last_compass_sent["dead-guid-1"] = 100.0
        _last_compass_sent["dead-guid-2"] = 200.0

        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        self.assertEqual(_last_compass_sent, {})

    async def test_stale_guid_purged_when_suspect_no_longer_wanted(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """GUIDs in _last_compass_sent but not in current wanted set are purged."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        _last_compass_sent["stale-guid"] = 100.0
        _last_compass_sent[criminal.guid] = time.monotonic() - 60

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 3000),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        self.assertNotIn("stale-guid", _last_compass_sent)
        self.assertIn(criminal.guid, _last_compass_sent)

    # -------------------------------------------------------------------
    # Multiple suspects — independent throttle
    # -------------------------------------------------------------------

    async def test_two_suspects_different_speeds_independent_throttle(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Two suspects at different speeds → fast one sent, slow one not."""
        criminal_fast = await self._setup_criminal()
        criminal_slow = await self._setup_criminal()
        officer = await self._setup_police()

        loc_fast = (100000, 100000, 0)
        loc_slow = (50000, 50000, 0)

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal_fast.player.unique_id, criminal_fast.guid, *loc_fast),
            _make_player_data(criminal_slow.player.unique_id, criminal_slow.guid, *loc_slow),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal_fast.guid, 3000),
            _make_mgmt_entry(criminal_slow.guid, 300),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        msg = mock_sys_msg.call_args.args[1]
        self.assertIn(criminal_fast.name, msg)
        self.assertNotIn(criminal_slow.name, msg)

    async def test_two_fast_suspects_both_sent(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Two fast suspects → both sent in same message."""
        criminal_a = await self._setup_criminal()
        criminal_b = await self._setup_criminal()
        officer = await self._setup_police()

        loc_a = (100000, 0, 0)
        loc_b = (0, 100000, 0)

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal_a.player.unique_id, criminal_a.guid, *loc_a),
            _make_player_data(criminal_b.player.unique_id, criminal_b.guid, *loc_b),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal_a.guid, 3000),
            _make_mgmt_entry(criminal_b.guid, 3000),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        msg = mock_sys_msg.call_args.args[1]
        self.assertIn(criminal_a.name, msg)
        self.assertIn(criminal_b.name, msg)

    async def test_two_suspects_one_throttled_one_not(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Two fast suspects: one was just sent (within interval), one hasn't →
        only the un-throttled one appears."""
        criminal_a = await self._setup_criminal()
        criminal_b = await self._setup_criminal()
        officer = await self._setup_police()

        loc_a = (100000, 0, 0)
        loc_b = (0, 100000, 0)

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal_a.player.unique_id, criminal_a.guid, *loc_a),
            _make_player_data(criminal_b.player.unique_id, criminal_b.guid, *loc_b),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal_a.guid, 3000),
            _make_mgmt_entry(criminal_b.guid, 3000),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        # First tick — both sent
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)
        self.assertIn(criminal_a.guid, _last_compass_sent)
        self.assertIn(criminal_b.guid, _last_compass_sent)

        # Backdate only criminal_b so its interval elapsed
        _last_compass_sent[criminal_b.guid] = time.monotonic() - 11

        mock_sys_msg.reset_mock()
        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        msg = mock_sys_msg.call_args.args[1]
        self.assertNotIn(criminal_a.name, msg)
        self.assertIn(criminal_b.name, msg)

    # -------------------------------------------------------------------
    # Multiple officers — each gets compass
    # -------------------------------------------------------------------

    async def test_two_officers_both_receive_compass(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Two officers both beyond 500m → both receive compass messages."""
        criminal = await self._setup_criminal()
        officer_a = await self._setup_police()
        officer_b = await self._setup_police()

        cop_a_loc = (0, 0, 0)
        cop_b_loc = (10000, 0, 0)

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer_a.player.unique_id, officer_a.guid, *cop_a_loc),
            _make_player_data(officer_b.player.unique_id, officer_b.guid, *cop_b_loc),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 3000),
        ]
        mock_police.return_value = _AsyncList([officer_a, officer_b])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        self.assertEqual(mock_sys_msg.await_count, 2)
        for call in mock_sys_msg.call_args_list:
            msg = call.args[1]
            self.assertIn(criminal.name, msg)

    async def test_two_officers_one_near_hides_from_both(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """One officer near suspect, one far → suspect hidden from BOTH."""
        criminal = await self._setup_criminal()
        officer_near = await self._setup_police()
        officer_far = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer_near.player.unique_id, officer_near.guid, *_COMPASS_COP_CLOSE),
            _make_player_data(officer_far.player.unique_id, officer_far.guid, *_COMPASS_COP_FAR),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 3000),
        ]
        mock_police.return_value = _AsyncList([officer_near, officer_far])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_not_called()

    # -------------------------------------------------------------------
    # Officer-suspect same GUID guard
    # -------------------------------------------------------------------

    async def test_officer_and_suspect_same_guid_skipped(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """If an officer has the same GUID as a suspect, that entry is skipped."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
        )
        await character.asave(update_fields=["last_online"])
        await Wanted.objects.acreate(character=character, wanted_remaining=300)
        await PoliceSession.objects.acreate(character=character)

        mock_get_players.return_value = _make_players_list([
            _make_player_data(player.unique_id, character.guid, *_COMPASS_SUSPECT_LOC),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(character.guid, 3000),
        ]
        mock_police.return_value = _AsyncList([character])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        mock_sys_msg.assert_not_called()

    # -------------------------------------------------------------------
    # Message content format
    # -------------------------------------------------------------------

    async def test_message_contains_distance_and_direction(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Compass message includes distance and cardinal direction."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        officer_loc = (0, 0, 0)
        suspect_loc = (100000, 0, 0)  # 1000m east

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *suspect_loc),
            _make_player_data(officer.player.unique_id, officer.guid, *officer_loc),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 3000),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        msg = mock_sys_msg.call_args.args[1]
        self.assertIn(criminal.name, msg)
        self.assertIn("E", msg)

    async def test_message_within_100m(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Suspect within 100m of an officer is hidden by proximity filter (500m).

        The "within 100m" message path in the code is unreachable in practice
        because proximity_hide (500m) > display threshold (100m). This test
        verifies that a close suspect is correctly hidden.
        """
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        officer_loc = (0, 0, 0)
        suspect_loc = (5000, 0, 0)  # 50m — within proximity hide (500m)

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *suspect_loc),
            _make_player_data(officer.player.unique_id, officer.guid, *officer_loc),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 3000),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        # Hidden by proximity — no message sent
        mock_sys_msg.assert_not_called()

    async def test_message_km_format_for_large_distances(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """Distance > 1000m shows km format."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        officer_loc = (0, 0, 0)
        suspect_loc = (200000, 0, 0)  # 2000m = 2.0km

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *suspect_loc),
            _make_player_data(officer.player.unique_id, officer.guid, *officer_loc),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 3000),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        msg = mock_sys_msg.call_args.args[1]
        self.assertIn("km", msg)

    # -------------------------------------------------------------------
    # send_system_message failure is graceful
    # -------------------------------------------------------------------

    async def test_send_failure_does_not_crash(
        self, mock_get_players, mock_get_locations, mock_police, mock_sys_msg,
    ):
        """send_system_message raising does not crash the tick."""
        criminal = await self._setup_criminal()
        officer = await self._setup_police()

        mock_get_players.return_value = _make_players_list([
            _make_player_data(criminal.player.unique_id, criminal.guid, *_COMPASS_SUSPECT_LOC),
            _make_player_data(officer.player.unique_id, officer.guid, *_COMPASS_COP_FAR),
        ])
        mock_get_locations.return_value = [
            _make_mgmt_entry(criminal.guid, 3000),
        ]
        mock_police.return_value = _AsyncList([officer])
        mock_sys_msg.side_effect = Exception("connection lost")
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        mock_http_mgmt = AsyncMock()

        await tick_police_suspect_locations(mock_http, mock_http_mod, mock_http_mgmt)

        self.assertIn(criminal.guid, _last_compass_sent)
