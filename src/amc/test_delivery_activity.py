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
from unittest.mock import AsyncMock, patch

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
    Character,
    DeliveryActivityLog,
    DeliveryJob,
    DeliveryPoint,
    Player,
    ServerCargoArrivedLog,
)
from amc.commands.jobs import (
    cmd_jobs,
    format_active_workers,
    get_active_workers_by_job,
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


class _ActivityTestBase(TestCase):
    """Fixture helpers plus explicit cleanup.

    Async ORM writes COMMIT (the test transaction wraps the sync connection,
    not the async one), so anything created here survives the test and would
    corrupt the many global-count assertions elsewhere in the suite
    (``DeliveryJob.objects.count()``, webhook delivery counts, ...).  Every
    fixture PK is tracked and deleted in ``tearDown``.
    """

    def setUp(self):
        delivery_telemetry._snapshot_deliveries = {}
        delivery_telemetry._snapshot_points = {}
        delivery_telemetry._snapshot_fetched_at = None
        self._tracked_players = []
        self._tracked_characters = []
        self._tracked_points = []
        self._tracked_jobs = []

    def tearDown(self):
        asyncio.run(self._cleanup())
        delivery_telemetry._snapshot_deliveries = {}
        delivery_telemetry._snapshot_points = {}
        delivery_telemetry._snapshot_fetched_at = None

    async def _cleanup(self):
        if self._tracked_characters:
            await DeliveryActivityLog.objects.filter(
                character_id__in=self._tracked_characters
            ).adelete()
            await ServerCargoArrivedLog.objects.filter(
                character_id__in=self._tracked_characters
            ).adelete()
        if self._tracked_jobs:
            await DeliveryJob.objects.filter(pk__in=self._tracked_jobs).adelete()
        if self._tracked_points:
            await DeliveryPoint.objects.filter(guid__in=self._tracked_points).adelete()
        if self._tracked_characters:
            await Character.objects.filter(pk__in=self._tracked_characters).adelete()
        if self._tracked_players:
            await Player.objects.filter(pk__in=self._tracked_players).adelete()

    # -- helpers (async: they run inside the test's event loop) -------------

    async def _character(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        self._tracked_players.append(player.pk)
        self._tracked_characters.append(character.pk)
        return player, character

    async def _points(self):
        # Unique PKs: a leaked point would break the next test's create.
        sender_guid = uuid.uuid4().hex[:12].upper()
        receiver_guid = uuid.uuid4().hex[:12].upper()
        sender = await DeliveryPoint.objects.acreate(
            guid=sender_guid, name="mine", type="mine", coord=Point(0, 0, 0)
        )
        receiver = await DeliveryPoint.objects.acreate(
            guid=receiver_guid,
            name="factory",
            type="factory",
            coord=Point(1000, 1000, 0),
        )
        self._tracked_points.extend([sender_guid, receiver_guid])
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
        self._tracked_jobs.append(job.pk)
        return job


class DeliveryActivityTests(_ActivityTestBase):
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


class DeliveryActivityWebhookTests(_ActivityTestBase):
    """The registration path: process_event must dispatch the new hook."""

    def test_accept_delivery_hook_is_registered(self):
        asyncio.run(self._accept_delivery_hook_is_registered())

    async def _accept_delivery_hook_is_registered(self):
        player, character = await self._character()
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


class _StubContext:
    """Minimal CommandContext for /jobs rendering."""

    def __init__(self):
        self.timestamp = timezone.now()
        self.character = None
        self.http_client_mod = None
        self.messages = []

    async def reply(self, message):
        self.messages.append(message)


class JobsPopupActivityTests(_ActivityTestBase):
    """/jobs shows who is on a job right now."""

    def test_active_workers_helper_groups_by_job(self):
        asyncio.run(self._active_workers_helper_groups_by_job())

    async def _active_workers_helper_groups_by_job(self):
        player, character = await self._character()
        sender, receiver = await self._points()
        job = await self._job(sender, receiver)
        now = timezone.now()
        await DeliveryActivityLog.objects.acreate(
            timestamp=now,
            kind="accepted",
            player=player,
            character=character,
            delivery_id=7001,
            job=job,
        )
        await DeliveryActivityLog.objects.acreate(
            timestamp=now,
            kind="picked_up",
            player=player,
            character=character,
            delivery_id=7001,
            job=job,
        )
        # Same hauler, second haul of the same job.
        await DeliveryActivityLog.objects.acreate(
            timestamp=now,
            kind="picked_up",
            player=player,
            character=character,
            delivery_id=7002,
            job=job,
        )

        workers = await get_active_workers_by_job()

        entry = workers[job.pk]
        self.assertEqual(list(entry["names"].values()), [character.name])
        self.assertEqual(entry["deliveries"], {7001, 7002})

    def test_format_caps_names(self):
        entry = {
            "names": {1: "Ana", 2: "Bob", 3: "Cid", 4: "Dee"},
            "deliveries": {1, 2},
        }
        line = format_active_workers(entry)
        self.assertIn("Ana, Bob, Cid +1", line)
        self.assertIn("2 in transit", line)

    def test_cmd_jobs_shows_working_line(self):
        asyncio.run(self._cmd_jobs_shows_working_line())

    async def _cmd_jobs_shows_working_line(self):
        from unittest.mock import MagicMock

        player, character = await self._character()
        sender, receiver = await self._points()
        job = await self._job(sender, receiver)
        await DeliveryActivityLog.objects.acreate(
            timestamp=timezone.now(),
            kind="picked_up",
            player=player,
            character=character,
            delivery_id=7003,
            job=job,
        )

        config = MagicMock()
        config.treasury_equilibrium = 1_000_000.0
        config.treasury_sensitivity = 1.0
        config.treasury_cap_ratio = 2.0
        ctx = _StubContext()
        with (
            patch(
                "amc.commands.jobs.JobPostingConfig.aget_config",
                new=AsyncMock(return_value=config),
            ),
            patch(
                "amc.commands.jobs.get_treasury_fund_balance",
                new=AsyncMock(return_value=1_000_000),
            ),
        ):
            await cmd_jobs(ctx)

        self.assertEqual(len(ctx.messages), 1)
        self.assertIn("Working: ", ctx.messages[0])
        self.assertIn(character.name, ctx.messages[0])
