"""Tests for the shared positions broadcaster.

All position streams (SSE positions, SSE count, WebSocket) must consume the
same per-tick snapshot produced by ONE loop — no per-connection fetch loops.
"""

import asyncio

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
    if b._session is not None:
        await b._session.close()


class StreamMaskedTests(SimpleTestCase):
    async def test_subscribers_share_one_fetch_per_tick(self):
        calls = 0

        async def fetch(session):
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
        async def fetch(session):
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

        async def fetch(session):
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
        async def fetch(session):
            return [_make_player(1), _make_player(2, hidden=True)]

        b = PositionsBroadcaster(fetch=fetch, sleep_s=0.01)
        try:
            gen = b.stream_count()
            self.assertEqual(await gen.__anext__(), "data: 1\n\n")
        finally:
            await _stop_broadcaster(b)

    async def test_count_stable_then_heartbeat(self):
        async def fetch(session):
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

        async def fetch(session):
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
