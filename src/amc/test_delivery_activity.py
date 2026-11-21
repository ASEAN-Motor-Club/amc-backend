"""Delivery activity tracking — accept / pickup rows and job attribution.

Async bodies are driven with ``asyncio.run`` and every DB write/read happens
INSIDE that coroutine.  pytest-asyncio runs in strict mode in this repo (async
``test_*`` methods are silently not executed), and the async ORM uses the
thread-sensitive executor connection — so a fixture created on the sync test
connection is invisible there (FK violation / empty queryset).  Writes made in
the coroutine commit rather than roll back with the test transaction, so every
assertion is scoped by this test's own ``delivery_id``.
"""

import asyncio
import datetime
import time
import uuid

from asgiref.sync import sync_to_async
from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone

from amc import delivery_telemetry
from amc.factories import CharacterFactory, PlayerFactory
from amc.handlers.delivery_activity import (
    handle_accept_delivery,
    record_cargo_pickup,
    stamp_job_for_delivery_ids,
)
from amc.models import (
    DeliveryActivityLog,
    DeliveryJob,
    DeliveryPoint,
    ServerCargoArrivedLog,
)
from amc.webhook import process_event


def _snapshot(delivery_id, cargo_key, sender_guid, receiver_guid):
    return {
        "data": [
            {
                "DeliveryPointGuid": sender_guid,
                "PaymentMultiplier": 1.0,
                "BasePayment": 500,
                "DemandConfigs": [],
                "Net_Deliveries": [
                    {
                        "ID": delivery_id,
                        "CargoKey": cargo_key,
                        "NumCargos": 4,
                        "Weight": 1200,
                        "SenderPoint": sender_guid,
                        "ReceiverPoint": receiver_guid,
                    }
                ],
            }
        ]
    }


class DeliveryActivityTests(TestCase):
    def setUp(self):
        delivery_telemetry._snapshot_deliveries = {}
        delivery_telemetry._snapshot_points = {}
        delivery_telemetry._snapshot_fetched_at = None

    def tearDown(self):
        delivery_telemetry._snapshot_deliveries = {}
        delivery_telemetry._snapshot_points = {}
        delivery_telemetry._snapshot_fetched_at = None

    # -- helpers (async: they run inside the test's event loop) -------------

    async def _character(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        return player, character

    async def _points(self):
        # Async writes commit (the test transaction does not wrap the async
        # connection), so fixture PKs must be unique per test.
        sender_guid = uuid.uuid4().hex[:12]
        receiver_guid = uuid.uuid4().hex[:12]
        sender = await DeliveryPoint.objects.acreate(
            guid=sender_guid.upper(), name="mine", type="mine", coord=Point(0, 0, 0)
        )
        receiver = await DeliveryPoint.objects.acreate(
            guid=receiver_guid.upper(),
            name="factory",
            type="factory",
            coord=Point(1000, 1000, 0),
        )
        return sender, receiver

    async def _job(self, sender, receiver, cargo_key="Milk"):
        job = await DeliveryJob.objects.acreate(
            name="Milk run",
            cargo_key=cargo_key,
            quantity_requested=100,
            expired_at=timezone.now() + datetime.timedelta(hours=3),
        )
        await job.source_points.aadd(sender)
        await job.destination_points.aadd(receiver)
        return job

    @staticmethod
    def _accept_event(delivery_id, player):
        return {
            "hook": "ServerAcceptDelivery",
            "timestamp": int(time.time()),
            "data": {"DeliveryId": delivery_id, "PlayerId": str(player.unique_id)},
        }

    # -- tests --------------------------------------------------------------

    def test_accept_records_row_with_job_from_snapshot(self):
        asyncio.run(self._accept_records_row_with_job_from_snapshot())

    async def _accept_records_row_with_job_from_snapshot(self):
        player, character = await self._character()
        sender, receiver = await self._points()
        job = await self._job(sender, receiver)
        delivery_telemetry.apply_snapshot(
            _snapshot(5001, "Milk", sender.guid, receiver.guid)
        )

        await handle_accept_delivery(
            self._accept_event(5001, player), player, character, None
        )

        row = await DeliveryActivityLog.objects.filter(delivery_id=5001).aget()
        self.assertEqual(row.kind, "accepted")
        self.assertEqual(row.cargo_key, "Milk")
        self.assertEqual(row.character_id, character.pk)
        self.assertEqual(row.sender_point_id, sender.pk)
        self.assertEqual(row.destination_point_id, receiver.pk)
        self.assertEqual(row.job_id, job.pk)

    def test_accept_is_deduped_within_window(self):
        asyncio.run(self._accept_is_deduped_within_window())

    async def _accept_is_deduped_within_window(self):
        player, character = await self._character()
        event = self._accept_event(5002, player)
        await handle_accept_delivery(event, player, character, None)
        await handle_accept_delivery(event, player, character, None)

        self.assertEqual(
            await DeliveryActivityLog.objects.filter(delivery_id=5002).acount(), 1
        )

    def test_accept_without_snapshot_still_logs(self):
        """An unknown delivery id must not raise (the snapshot can be stale)."""
        asyncio.run(self._accept_without_snapshot_still_logs())

    async def _accept_without_snapshot_still_logs(self):
        player, character = await self._character()
        await handle_accept_delivery(
            self._accept_event(5003, player), player, character, None
        )

        row = await DeliveryActivityLog.objects.filter(delivery_id=5003).aget()
        self.assertIsNone(row.cargo_key)
        self.assertIsNone(row.job_id)

    def test_zero_delivery_id_is_ignored(self):
        asyncio.run(self._zero_delivery_id_is_ignored())

    async def _zero_delivery_id_is_ignored(self):
        player, character = await self._character()
        await handle_accept_delivery(
            self._accept_event(0, player), player, character, None
        )
        self.assertEqual(
            await DeliveryActivityLog.objects.filter(delivery_id=0).acount(), 0
        )

    def test_pickup_recorded_for_non_police(self):
        """The pickup hook is owned by the police handler — telemetry must fire
        for ordinary haulers, not just confiscations."""
        asyncio.run(self._pickup_recorded_for_non_police())

    async def _pickup_recorded_for_non_police(self):
        player, character = await self._character()
        sender, receiver = await self._points()
        await self._job(sender, receiver)
        delivery_telemetry.apply_snapshot(
            _snapshot(5004, "Milk", sender.guid, receiver.guid)
        )

        event = {
            "hook": "ServerPickupCargo",
            "timestamp": int(time.time()),
            "data": {
                "Cargo": {
                    "Net_CargoKey": "Milk",
                    "Net_DeliveryId": 5004,
                    "Net_Payment": 720,
                    "Net_Weight": 1200.0,
                },
                "PlayerId": str(player.unique_id),
                "CharacterGuid": str(character.guid),
            },
        }
        await record_cargo_pickup(event, player, character)

        row = await DeliveryActivityLog.objects.filter(delivery_id=5004).aget()
        self.assertEqual(row.kind, "picked_up")
        self.assertEqual(row.cargo_key, "Milk")
        self.assertEqual(row.payment, 720)
        self.assertEqual(row.weight, 1200.0)

    def test_pickup_without_delivery_id_is_ignored(self):
        """Non-job cargo (free roam / own goods) has DeliveryId 0."""
        asyncio.run(self._pickup_without_delivery_id_is_ignored())

    async def _pickup_without_delivery_id_is_ignored(self):
        player, character = await self._character()
        event = {
            "hook": "ServerPickupCargo",
            "timestamp": int(time.time()),
            "data": {"Cargo": {"Net_CargoKey": "Milk", "Net_DeliveryId": 0}},
        }
        await record_cargo_pickup(event, player, character)
        self.assertEqual(
            await DeliveryActivityLog.objects.filter(delivery_id=0).acount(), 0
        )

    def test_arrival_stamps_job_onto_earlier_rows(self):
        asyncio.run(self._arrival_stamps_job_onto_earlier_rows())

    async def _arrival_stamps_job_onto_earlier_rows(self):
        player, character = await self._character()
        await DeliveryActivityLog.objects.acreate(
            timestamp=timezone.now(),
            kind="accepted",
            player=player,
            character=character,
            delivery_id=5005,
        )
        sender, receiver = await self._points()
        job = await self._job(sender, receiver)

        updated = await stamp_job_for_delivery_ids([5005], job.pk)

        self.assertEqual(updated, 1)
        row = await DeliveryActivityLog.objects.filter(delivery_id=5005).aget()
        self.assertEqual(row.job_id, job.pk)

    def test_active_excludes_delivered_deliveries(self):
        asyncio.run(self._active_excludes_delivered_deliveries())

    async def _active_excludes_delivered_deliveries(self):
        player, character = await self._character()
        now = timezone.now()
        await DeliveryActivityLog.objects.acreate(
            timestamp=now,
            kind="picked_up",
            player=player,
            character=character,
            delivery_id=5006,
        )
        await DeliveryActivityLog.objects.acreate(
            timestamp=now,
            kind="picked_up",
            player=player,
            character=character,
            delivery_id=5007,
        )
        await ServerCargoArrivedLog.objects.acreate(
            timestamp=now,
            player=player,
            character=character,
            cargo_key="Milk",
            payment=100,
            delivery_id=5007,
        )

        active = [
            row.delivery_id
            async for row in DeliveryActivityLog.objects.filter(
                delivery_id__in=[5006, 5007]
            ).active(60)
        ]
        self.assertEqual(active, [5006])


class DeliveryActivityWebhookTests(TestCase):
    """The registration path: process_event must dispatch the new hook."""

    def test_accept_delivery_hook_is_registered(self):
        asyncio.run(self._accept_delivery_hook_is_registered())

    async def _accept_delivery_hook_is_registered(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        event = {
            "hook": "ServerAcceptDelivery",
            "timestamp": int(time.time()),
            "data": {"DeliveryId": 6001, "PlayerId": str(player.unique_id)},
        }
        await process_event(
            event,
            player,
            character,
            is_rp_mode=False,
            treasury_balance=100_000,
        )

        row = await DeliveryActivityLog.objects.filter(delivery_id=6001).aget()
        self.assertEqual(row.delivery_id, 6001)
