"""Tests for player position filtering logic in amc.api.player_positions_common.

Covers the filter_hidden parameter of get_players_mod:
- Wanted criminals are excluded when filter_hidden=True.
- Costume criminals (wearing_costume=True) are excluded.
- Police officers are excluded when filter_hidden=True AND an active wanted criminal exists.
- Police officers are included when filter_hidden=True but NO active wanted criminal exists.
- Regular players are always included.
- Default filter_hidden=False returns all players unchanged (no DB queries needed).
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from amc.api.player_positions_common import (
    _mask_hidden_player,
    _should_hide_player,
    get_players_mod,
    get_players_mod_masked,
)
from amc.api.player_positions_pb2 import PlayerPositions
from amc.api.player_positions_ws import _VEHICLE_KEY_MAP, serialize_players
from amc.factories import CharacterFactory, PlayerFactory
from amc.models import PoliceSession, Wanted


def _make_mod_player(unique_id, player_name="TestPlayer", x=0, y=0, z=0, vehicle_key=""):
    return {
        "UniqueID": str(unique_id),
        "PlayerName": player_name,
        "Location": {"X": x, "Y": y, "Z": z},
        "VehicleKey": vehicle_key,
    }


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    async def json(self):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class _FakeSession:
    def __init__(self, players):
        self._players = players

    @asynccontextmanager
    async def get(self, path):
        yield _FakeResponse({"data": self._players})


class ShouldHidePlayerTests(TestCase):
    def test_wanted_player_is_hidden(self):
        p = _make_mod_player(42)
        self.assertTrue(_should_hide_player(p, {42}, set(), set(), True))

    def test_wanted_player_hidden_even_without_any_wanted_flag(self):
        p = _make_mod_player(42)
        self.assertTrue(_should_hide_player(p, {42}, set(), set(), False))

    def test_police_hidden_when_any_wanted(self):
        p = _make_mod_player(99)
        self.assertTrue(_should_hide_player(p, set(), {99}, set(), True))

    def test_police_not_hidden_when_no_wanted(self):
        p = _make_mod_player(99)
        self.assertFalse(_should_hide_player(p, set(), {99}, set(), False))

    def test_costume_criminal_is_hidden(self):
        p = _make_mod_player(55)
        self.assertTrue(_should_hide_player(p, set(), set(), {55}, False))

    def test_costume_criminal_hidden_even_without_wanted(self):
        p = _make_mod_player(55)
        self.assertTrue(_should_hide_player(p, set(), set(), {55}, False))

    def test_regular_player_not_hidden(self):
        p = _make_mod_player(7)
        self.assertFalse(_should_hide_player(p, {42}, {99}, {55}, True))

    def test_malformed_unique_id_not_hidden(self):
        p = {"UniqueID": "not_a_number", "PlayerName": "Bad"}
        self.assertFalse(_should_hide_player(p, {42}, {99}, set(), True))

    def test_missing_unique_id_not_hidden(self):
        p = {"PlayerName": "NoID"}
        self.assertFalse(_should_hide_player(p, {42}, {99}, set(), True))


class GetPlayersModFilterTests(TestCase):
    def setUp(self):
        cache.clear()

    async def _setup_wanted_criminal(self, wanted_remaining=300):
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
        return player, character

    async def _setup_police(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
        )
        await character.asave(update_fields=["last_online"])
        await PoliceSession.objects.acreate(character=character)
        return player, character

    async def _setup_regular_player(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
        )
        await character.asave(update_fields=["last_online"])
        return player, character

    async def _setup_costume_criminal(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
            wearing_costume=True,
            costume_item_key="Costume_Butcher_01",
        )
        await character.asave(update_fields=["last_online", "wearing_costume", "costume_item_key"])
        return player, character

    async def test_filter_false_returns_all(self):
        criminal_player, _ = await self._setup_wanted_criminal()
        session = _FakeSession([_make_mod_player(criminal_player.unique_id)])
        result = await get_players_mod(session, filter_hidden=False)
        self.assertEqual(len(result), 1)

    async def test_default_returns_all(self):
        criminal_player, _ = await self._setup_wanted_criminal()
        session = _FakeSession([_make_mod_player(criminal_player.unique_id)])
        result = await get_players_mod(session)
        self.assertEqual(len(result), 1)

    async def test_wanted_criminal_excluded(self):
        criminal_player, _ = await self._setup_wanted_criminal()
        session = _FakeSession([_make_mod_player(criminal_player.unique_id)])
        result = await get_players_mod(session, filter_hidden=True)
        self.assertEqual(len(result), 0)

    async def test_police_excluded_when_wanted_exists(self):
        criminal_player, _ = await self._setup_wanted_criminal()
        police_player, _ = await self._setup_police()
        session = _FakeSession([
            _make_mod_player(criminal_player.unique_id),
            _make_mod_player(police_player.unique_id),
        ])
        result = await get_players_mod(session, filter_hidden=True)
        self.assertEqual(len(result), 0)

    async def test_police_included_when_no_wanted(self):
        police_player, _ = await self._setup_police()
        session = _FakeSession([_make_mod_player(police_player.unique_id)])
        result = await get_players_mod(session, filter_hidden=True)
        self.assertEqual(len(result), 1)

    async def test_regular_player_always_included(self):
        criminal_player, _ = await self._setup_wanted_criminal()
        police_player, _ = await self._setup_police()
        regular_player, _ = await self._setup_regular_player()
        session = _FakeSession([
            _make_mod_player(criminal_player.unique_id),
            _make_mod_player(police_player.unique_id),
            _make_mod_player(regular_player.unique_id),
        ])
        result = await get_players_mod(session, filter_hidden=True)
        uids = [int(p["UniqueID"]) for p in result]
        self.assertIn(regular_player.unique_id, uids)
        self.assertNotIn(criminal_player.unique_id, uids)
        self.assertNotIn(police_player.unique_id, uids)
        self.assertEqual(len(result), 1)

    async def test_expired_wanted_not_hidden(self):
        criminal_player, criminal_char = await self._setup_wanted_criminal()
        w = await Wanted.objects.aget(character=criminal_char)
        w.wanted_remaining = 0
        w.expired_at = timezone.now()
        await w.asave(update_fields=["wanted_remaining", "expired_at"])
        session = _FakeSession([_make_mod_player(criminal_player.unique_id)])
        result = await get_players_mod(session, filter_hidden=True)
        self.assertEqual(len(result), 1)

    async def test_ended_police_session_not_hidden(self):
        _, police_char = await self._setup_police()
        await PoliceSession.objects.filter(
            character=police_char, ended_at__isnull=True
        ).aupdate(ended_at=timezone.now())
        police_player = police_char.player
        session = _FakeSession([_make_mod_player(police_player.unique_id)])
        result = await get_players_mod(session, filter_hidden=True)
        self.assertEqual(len(result), 1)

    async def test_cached_data_filtered(self):
        criminal_player, _ = await self._setup_wanted_criminal()
        regular_player, _ = await self._setup_regular_player()
        players_data = [
            _make_mod_player(criminal_player.unique_id),
            _make_mod_player(regular_player.unique_id),
        ]
        cache.set("mod_players_list_all", players_data, timeout=5)
        session = _FakeSession([])
        result = await get_players_mod(session, filter_hidden=True)
        self.assertEqual(len(result), 1)
        self.assertEqual(int(result[0]["UniqueID"]), regular_player.unique_id)

    async def test_use_cache_false_bypasses_cache(self):
        """With use_cache=False the roster is fetched directly even when the
        cache holds data, and the fetch is not written back to the cache."""
        cache.set("mod_players_list_all", [_make_mod_player(999)], timeout=5)
        session = _FakeSession([_make_mod_player(1)])
        result = await get_players_mod(session, use_cache=False)
        self.assertEqual([int(p["UniqueID"]) for p in result], [1])
        # the fetch is not written back — the cache still holds only the stale
        # entry we seeded, never the fresh one
        cached = cache.get("mod_players_list_all")
        self.assertEqual([int(p["UniqueID"]) for p in cached], [999])

    async def test_masked_use_cache_false_fresh_snapshot(self):
        cache.set("mod_players_list_all", [_make_mod_player(999)], timeout=5)
        session = _FakeSession([_make_mod_player(7)])
        result = await get_players_mod_masked(session, use_cache=False)
        self.assertEqual([int(p["UniqueID"]) for p in result], [7])

    async def test_no_db_query_when_filter_false_and_cached(self):
        cache.set("mod_players_list_all", [_make_mod_player(1)], timeout=5)
        session = _FakeSession([])
        with patch(
            "amc.api.player_positions_common._get_hidden_player_unique_ids",
            new_callable=AsyncMock,
        ) as mock_hidden:
            result = await get_players_mod(session, filter_hidden=False)
            mock_hidden.assert_not_called()
            self.assertEqual(len(result), 1)

    async def test_empty_players_returns_empty(self):
        session = _FakeSession([])
        result = await get_players_mod(session, filter_hidden=True)
        self.assertEqual(result, [])

    async def test_malformed_unique_id_not_crash(self):
        await self._setup_wanted_criminal()
        session = _FakeSession([
            {"UniqueID": "bad", "PlayerName": "Bad", "Location": {"X": 0, "Y": 0, "Z": 0}, "VehicleKey": ""},
        ])
        result = await get_players_mod(session, filter_hidden=True)
        self.assertEqual(len(result), 1)

    async def test_costume_criminal_excluded(self):
        costume_player, _ = await self._setup_costume_criminal()
        session = _FakeSession([_make_mod_player(costume_player.unique_id)])
        result = await get_players_mod(session, filter_hidden=True)
        self.assertEqual(len(result), 0)

    async def test_costume_criminal_excluded_without_wanted(self):
        """Costume criminals are hidden even when no wanted criminals exist."""
        costume_player, _ = await self._setup_costume_criminal()
        regular_player, _ = await self._setup_regular_player()
        session = _FakeSession([
            _make_mod_player(costume_player.unique_id),
            _make_mod_player(regular_player.unique_id),
        ])
        result = await get_players_mod(session, filter_hidden=True)
        self.assertEqual(len(result), 1)
        self.assertEqual(int(result[0]["UniqueID"]), regular_player.unique_id)

    async def test_costume_removed_not_hidden(self):
        """A player who removed their costume should be visible."""
        costume_player, costume_char = await self._setup_costume_criminal()
        costume_char.wearing_costume = False
        costume_char.costume_item_key = None
        await costume_char.asave(update_fields=["wearing_costume", "costume_item_key"])
        session = _FakeSession([_make_mod_player(costume_player.unique_id)])
        result = await get_players_mod(session, filter_hidden=True)
        self.assertEqual(len(result), 1)


class GetPlayersModMaskedTests(TestCase):
    """get_players_mod_masked keeps hidden players in the list but zeroes
    their location/vehicle and flags them hidden=True."""

    def setUp(self):
        cache.clear()

    async def _setup_wanted_criminal(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
        )
        await character.asave(update_fields=["last_online"])
        await Wanted.objects.acreate(
            character=character,
            wanted_remaining=300,
        )
        return player, character

    async def _setup_police(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
        )
        await character.asave(update_fields=["last_online"])
        await PoliceSession.objects.acreate(character=character)
        return player, character

    async def _setup_regular_player(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
        )
        await character.asave(update_fields=["last_online"])
        return player, character

    async def _setup_costume_criminal(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
            wearing_costume=True,
            costume_item_key="Costume_Butcher_01",
        )
        await character.asave(update_fields=["last_online", "wearing_costume", "costume_item_key"])
        return player, character

    def _by_uid(self, result):
        return {int(p["UniqueID"]): p for p in result}

    async def test_masked_wanted_player_zeroed_and_flagged(self):
        criminal_player, _ = await self._setup_wanted_criminal()
        session = _FakeSession(
            [
                _make_mod_player(
                    criminal_player.unique_id, x=123, y=456, z=789, vehicle_key="DUKE"
                ),
            ]
        )
        result = await get_players_mod_masked(session)
        entry = self._by_uid(result)[criminal_player.unique_id]
        self.assertTrue(entry["hidden"])
        self.assertEqual(entry["Location"], {"X": 0.0, "Y": 0.0, "Z": 0.0})
        self.assertEqual(entry["VehicleKey"], "")

    async def test_masked_regular_player_keeps_position(self):
        regular_player, _ = await self._setup_regular_player()
        session = _FakeSession(
            [
                _make_mod_player(
                    regular_player.unique_id, x=123, y=456, z=789, vehicle_key="DUKE"
                ),
            ]
        )
        result = await get_players_mod_masked(session)
        entry = self._by_uid(result)[regular_player.unique_id]
        self.assertFalse(entry["hidden"])
        self.assertEqual(entry["Location"], {"X": 123, "Y": 456, "Z": 789})
        self.assertEqual(entry["VehicleKey"], "DUKE")

    async def test_masked_police_hidden_when_wanted_exists(self):
        criminal_player, _ = await self._setup_wanted_criminal()
        police_player, _ = await self._setup_police()
        session = _FakeSession(
            [
                _make_mod_player(criminal_player.unique_id, x=1, y=2, z=3),
                _make_mod_player(police_player.unique_id, x=4, y=5, z=6),
            ]
        )
        result = await get_players_mod_masked(session)
        by_uid = self._by_uid(result)
        self.assertTrue(by_uid[criminal_player.unique_id]["hidden"])
        self.assertTrue(by_uid[police_player.unique_id]["hidden"])

    async def test_masked_police_visible_when_no_wanted(self):
        police_player, _ = await self._setup_police()
        session = _FakeSession([_make_mod_player(police_player.unique_id)])
        result = await get_players_mod_masked(session)
        self.assertFalse(self._by_uid(result)[police_player.unique_id]["hidden"])

    async def test_masked_keeps_every_player_in_list(self):
        criminal_player, _ = await self._setup_wanted_criminal()
        police_player, _ = await self._setup_police()
        regular_player, _ = await self._setup_regular_player()
        session = _FakeSession(
            [
                _make_mod_player(criminal_player.unique_id),
                _make_mod_player(police_player.unique_id),
                _make_mod_player(regular_player.unique_id),
            ]
        )
        result = await get_players_mod_masked(session)
        self.assertEqual(len(result), 3)

    async def test_masked_costume_criminal_zeroed(self):
        costume_player, _ = await self._setup_costume_criminal()
        session = _FakeSession(
            [_make_mod_player(costume_player.unique_id, x=1, y=2, z=3)]
        )
        result = await get_players_mod_masked(session)
        entry = self._by_uid(result)[costume_player.unique_id]
        self.assertTrue(entry["hidden"])
        self.assertEqual(entry["Location"], {"X": 0.0, "Y": 0.0, "Z": 0.0})


class SerializePlayersHiddenTests(SimpleTestCase):
    def test_hidden_player_serializes_zeroed(self):
        # Single source of truth: the mask zeroes hidden entries; the
        # serializer just translates whatever get_players_mod_masked() hands it.
        masked = _mask_hidden_player(
            _make_mod_player(42, x=123, y=456, z=789, vehicle_key="DUKE")
        )
        data = serialize_players(
            [
                masked,
                _make_mod_player(7, x=1, y=2, z=3, vehicle_key="DUKE"),
            ],
            timestamp_s=1_700_000_000.5,
        )
        positions = PlayerPositions.FromString(data)
        self.assertEqual(positions.timestamp_ms, 1_700_000_000_500)
        positions = PlayerPositions.FromString(data)
        hidden, visible = positions.players
        self.assertTrue(hidden.hidden)
        self.assertEqual((hidden.x, hidden.y, hidden.z), (0.0, 0.0, 0.0))
        self.assertFalse(hidden.HasField("vehicle_key_enum"))
        self.assertTrue(hidden.HasField("vehicle_key_unknown"))
        self.assertEqual(hidden.vehicle_key_unknown, "")
        self.assertEqual(hidden.unique_id, 42)
        self.assertFalse(visible.hidden)
        self.assertEqual((visible.x, visible.y, visible.z), (1.0, 2.0, 3.0))
        self.assertEqual(visible.vehicle_key_enum, _VEHICLE_KEY_MAP["DUKE"])

    def test_velocity_serializes(self):
        """Velocity from the merged roster lands in the velocity field; entries
        without one (Lua fallback) leave it at the proto3 default (absent)."""
        moving = _make_mod_player(7, x=1, y=2, z=3)
        moving["Velocity"] = {"X": 4.5, "Y": -1.0, "Z": 0.5}
        masked = _mask_hidden_player(
            dict(_make_mod_player(42, x=123, y=456, z=789),
                 Velocity={"X": 9.0, "Y": 9.0, "Z": 9.0})
        )
        data = serialize_players([moving, masked], timestamp_s=0.0)
        positions = PlayerPositions.FromString(data)
        visible, hidden = positions.players
        self.assertEqual(
            (visible.velocity.x, visible.velocity.y, visible.velocity.z),
            (4.5, -1.0, 0.5),
        )
        self.assertEqual(
            (hidden.velocity.x, hidden.velocity.y, hidden.velocity.z),
            (0.0, 0.0, 0.0),
        )

    def test_velocity_absent_stays_default(self):
        """Lua-fallback entries (no Velocity key) leave the field absent."""
        data = serialize_players(
            [_make_mod_player(7, x=1, y=2, z=3)], timestamp_s=0.0
        )
        pos = PlayerPositions.FromString(data).players[0]
        self.assertFalse(pos.HasField("velocity"))
        self.assertEqual(pos.velocity.x, 0.0)
