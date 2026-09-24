"""Tests for the shared positions broadcaster.

All position streams (SSE positions, SSE count, WebSocket) must consume the
same per-tick snapshot produced by ONE loop — no per-connection fetch loops.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase

from amc.api.player_positions_common import HEARTBEAT_INTERVAL, POSITION_UPDATE_SLEEP
from amc.api.positions_broadcaster import PositionsBroadcaster


def _make_player(unique_id, x=1, y=2, z=3, vehicle_key="DUKE", hidden=False):
    return {
        "UniqueID": str(unique_id),
        "PlayerName": f"P{unique_id}",
        "Location": {"X": x, "Y": y, "Z": z},
        "VehicleKey": vehicle_key,
        "hidden": hidden,
    }


async def _stop_broadcaster(b):
    if b._task is not None and not b._task.done():
        b._task.cancel()
        try:
            await b._task
        except asyncio.CancelledError:
            pass
    for sess in (b._session, b._mgmt_session):
        if sess is not None:
            await sess.close()


class TickScheduleTests(SimpleTestCase):
    async def test_tick_period_does_not_drift_with_slow_fetch(self):
        """Tick N must start at ~N * interval after the previous tick start,
        not previous_end + interval — a slow fetch must not accumulate drift."""
        interval = 0.05
        fetch_durations = [interval / 2] * 4
        tick_starts: list[float] = []

        async def fetch(session, mgmt_session=None):
            # measured from tick start: fetch itself takes half the interval
            tick_starts.append(asyncio.get_running_loop().time())
            await asyncio.sleep(fetch_durations[len(tick_starts) - 1])
            return []

        b = PositionsBroadcaster(fetch=fetch, sleep_s=interval)
        b.ensure_started()
        g = b.stream_masked()
        for _ in range(4):
            await g.__anext__()
        await _stop_broadcaster(b)

        self.assertEqual(len(tick_starts), 4)
        periods = [
            t2 - t1 for t1, t2 in zip(tick_starts, tick_starts[1:])
        ]
        for period in periods:
            # absolute schedule: period ≈ interval, NOT interval + duration
            self.assertLess(period, interval * 1.25, periods)


class StreamMaskedTests(SimpleTestCase):
    async def test_subscribers_share_one_fetch_per_tick(self):
        calls = 0

        async def fetch(session, mgmt_session=None):
            nonlocal calls
            calls += 1
            return [_make_player(1, x=calls)]

        b = PositionsBroadcaster(fetch=fetch, sleep_s=0.01)
        try:
            g1 = b.stream_masked()
            g2 = b.stream_masked()
            s1, ts1 = await g1.__anext__()
            s2, ts2 = await g2.__anext__()
            self.assertIs(s1, s2)  # same snapshot generation
            self.assertEqual(ts1, ts2)  # same tick timestamp
            self.assertGreater(ts1, 0)  # tick time recorded
            self.assertEqual(calls, 1)  # one fetch, not one per subscriber

            await asyncio.sleep(0.05)  # let the next tick(s) run
            n1, nts1 = await g1.__anext__()
            n2, nts2 = await g2.__anext__()
            self.assertIs(n1, n2)  # still the same generation for both
            self.assertEqual(nts1, nts2)
            self.assertGreaterEqual(nts1, ts1)  # timestamps are monotonic
            self.assertGreaterEqual(calls, 2)
        finally:
            await _stop_broadcaster(b)

    async def test_first_snapshot_waited_not_empty(self):
        async def fetch(session, mgmt_session=None):
            return [_make_player(1)]

        b = PositionsBroadcaster(fetch=fetch, sleep_s=0.01)
        try:
            async for snapshot, ts in b.stream_masked():
                self.assertEqual(len(snapshot), 1)
                self.assertGreater(ts, 0)
                break
        finally:
            await _stop_broadcaster(b)

    async def test_fetch_failure_keeps_last_snapshot(self):
        state = {"fail": False}
        good = [_make_player(1)]

        async def fetch(session, mgmt_session=None):
            if state["fail"]:
                raise RuntimeError("mod server down")
            return good

        b = PositionsBroadcaster(fetch=fetch, sleep_s=0.01)
        try:
            gen = b.stream_masked()
            first, _ts = await gen.__anext__()
            state["fail"] = True
            await asyncio.sleep(0.08)  # several failing ticks
            self.assertFalse(b._task.done())  # loop survives
            self.assertEqual(b._snapshot, first)  # last good snapshot retained
            self.assertIs(first, good)
        finally:
            await _stop_broadcaster(b)


class StreamCountTests(SimpleTestCase):
    async def test_count_excludes_hidden_players(self):
        async def fetch(session, mgmt_session=None):
            return [_make_player(1), _make_player(2, hidden=True)]

        b = PositionsBroadcaster(fetch=fetch, sleep_s=0.01)
        try:
            gen = b.stream_count()
            self.assertEqual(await gen.__anext__(), "data: 1\n\n")
        finally:
            await _stop_broadcaster(b)

    async def test_count_stable_then_heartbeat(self):
        async def fetch(session, mgmt_session=None):
            return [_make_player(1)]

        b = PositionsBroadcaster(fetch=fetch, sleep_s=0.01)
        try:
            gen = b.stream_count()
            self.assertEqual(await gen.__anext__(), "data: 1\n\n")
            ticks_needed = int(-(-HEARTBEAT_INTERVAL // POSITION_UPDATE_SLEEP))
            for _ in range(ticks_needed):
                await asyncio.sleep(0.02)
            msg = await asyncio.wait_for(gen.__anext__(), timeout=5)
            self.assertEqual(msg, ": heartbeat\n\n")
        finally:
            await _stop_broadcaster(b)

    async def test_count_yields_on_change(self):
        calls = 0

        async def fetch(session, mgmt_session=None):
            nonlocal calls
            calls += 1
            return (
                [_make_player(1), _make_player(2)] if calls > 2 else [_make_player(1)]
            )

        b = PositionsBroadcaster(fetch=fetch, sleep_s=0.01)
        try:
            gen = b.stream_count()
            self.assertEqual(await gen.__anext__(), "data: 1\n\n")
            await asyncio.sleep(0.08)
            msg = await asyncio.wait_for(gen.__anext__(), timeout=5)
            self.assertEqual(msg, "data: 2\n\n")
        finally:
            await _stop_broadcaster(b)


class DefaultFetchTests(SimpleTestCase):
    async def test_default_fetch_c_cpp_source_and_lua_identity(self):
        """C++ feed available: location/vehicle from the management API,
        identity from the Lua roster (cache-read allowed — identity only)."""
        from amc.api.player_positions_common import get_positions_masked

        locations = [
            {
                "CharacterGuid": "GUID-1",
                "Location": {"X": 5, "Y": 6, "Z": 7},
                "VehicleKey": None,  # get_players_locations yields None when empty
            }
        ]
        identity = [
            {"CharacterGuid": "guid-1", "UniqueID": "42", "PlayerName": "P1"}
        ]
        hidden_ids = set()

        with (
            patch(
                "amc.api.player_positions_common.get_players_locations",
                new=AsyncMock(return_value=locations),
            ),
            patch(
                "amc.api.player_positions_common.get_players_mod",
                new=AsyncMock(return_value=identity),
            ),
            patch(
                "amc.api.player_positions_common._get_hidden_player_unique_ids",
                new=AsyncMock(return_value=(hidden_ids, set(), set())),
            ),
        ):
            roster = await get_positions_masked(None, None)
            self.assertEqual(len(roster), 1)
            self.assertEqual(roster[0]["UniqueID"], "42")
            self.assertEqual(roster[0]["PlayerName"], "P1")
            self.assertEqual(roster[0]["Location"], {"X": 5, "Y": 6, "Z": 7})
            self.assertEqual(roster[0]["VehicleKey"], "")
            self.assertFalse(roster[0]["hidden"])

    async def test_default_fetch_falls_back_to_lua_when_cpp_unavailable(self):
        """C++ feed down (None): fall back to the Lua-only masked path with
        the cache bypassed."""
        from amc.api.player_positions_common import get_positions_masked

        fallback = [{"UniqueID": "1", "PlayerName": "P1"}]
        with (
            patch(
                "amc.api.player_positions_common.get_players_locations",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "amc.api.player_positions_common.get_players_mod_masked",
                new=AsyncMock(return_value=fallback),
            ),
        ):
            roster = await get_positions_masked(None, None)
            self.assertIs(roster, fallback)

    async def test_default_fetch_wiring(self):
        """The broadcaster default fetch delegates to get_positions_masked."""
        from amc.api import positions_broadcaster

        merged = [{"UniqueID": "1"}]
        with patch.object(
            positions_broadcaster, "get_positions_masked",
            new=AsyncMock(return_value=merged),
        ) as mock_masked:
            out = await positions_broadcaster._default_fetch(None, None)
            self.assertIs(out, merged)
            mock_masked.assert_awaited_once_with(None, None)

    async def test_merged_roster_masks_hidden_player(self):
        """Hidden DB sets zero location/vehicle on the merged roster."""
        from amc.api.player_positions_common import _merge_masked_roster

        locations = [
            {"CharacterGuid": "G-WANTED", "Location": {"X": 9, "Y": 9, "Z": 9},
             "VehicleKey": "DUKE"},
            {"CharacterGuid": "G-OK", "Location": {"X": 1, "Y": 2, "Z": 3},
             "VehicleKey": None},
        ]
        identity = [
            {"CharacterGuid": "g-wanted", "UniqueID": "7", "PlayerName": "W"},
            {"CharacterGuid": "g-ok", "UniqueID": "8", "PlayerName": "O"},
        ]
        with patch(
            "amc.api.player_positions_common._get_hidden_player_unique_ids",
            new=AsyncMock(return_value=({7}, set(), set())),
        ):
            roster = await _merge_masked_roster(locations, identity)
        by_guid = {r["CharacterGuid"]: r for r in roster}
        wanted = by_guid["G-WANTED"]
        self.assertTrue(wanted["hidden"])
        self.assertEqual(wanted["Location"], {"X": 0.0, "Y": 0.0, "Z": 0.0})
        self.assertEqual(wanted["VehicleKey"], "")
        ok = by_guid["G-OK"]
        self.assertFalse(ok["hidden"])
        self.assertEqual(ok["Location"], {"X": 1, "Y": 2, "Z": 3})
        self.assertEqual(ok["PlayerName"], "O")
