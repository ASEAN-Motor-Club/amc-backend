import asyncio
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from amc.cargo import get_cargo_bonus
from amc.handlers.cargo import (
    _extract_delivery_id,
    _parse_cargos,
    _split_duplicate_deliveries,
)
from amc.models import Character, Player, ServerCargoArrivedLog


def _make_event(*cargos):
    """Helper: wrap a list of cargo dicts in a minimal ServerCargoArrived event."""
    return {"data": {"Cargos": list(cargos)}}


def _cargo(key="Wood", payment=1000, delivery_id=None):
    """Build a minimal cargo dict."""
    c = {"Net_CargoKey": key, "Net_Payment": payment, "Net_Damage": 0.0}
    if delivery_id is not None:
        c["Net_DeliveryId"] = delivery_id
    return c


class ParseCargosTests(TestCase):
    def test_normal_cargo_included(self):
        """Cargos with a real DeliveryId pass through unchanged."""
        event = _make_event(_cargo("Wood", 1000, delivery_id=42))
        result = _parse_cargos(event)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["Net_CargoKey"], "Wood")

    def test_zero_delivery_id_included(self):
        """Cargos with DeliveryId == 0 are now included — fraud detection still applies."""
        event = _make_event(_cargo("Wood", 5000, delivery_id=0))
        result = _parse_cargos(event)
        self.assertEqual(len(result), 1, "DeliveryId=0 cargo should be processed, not dropped")

    def test_mixed_cargos_all_returned(self):
        """Both DeliveryId=0 and real DeliveryId cargos are returned."""
        event = _make_event(
            _cargo("Coal", 2000, delivery_id=0),
            _cargo("Iron", 3000, delivery_id=99),
        )
        result = _parse_cargos(event)
        self.assertEqual(len(result), 2)
        keys = [c["Net_CargoKey"] for c in result]
        self.assertIn("Coal", keys)
        self.assertIn("Iron", keys)

    def test_cargo_without_delivery_id_field_included(self):
        """Cargos that have no Net_DeliveryId key at all are treated as normal."""
        event = _make_event(_cargo("Stone", 800))  # no delivery_id kwarg → key absent
        result = _parse_cargos(event)
        self.assertEqual(len(result), 1)

    def test_negative_payment_raises(self):
        """Negative payment is always a hard error regardless of DeliveryId."""
        event = _make_event(_cargo("Hack", -500, delivery_id=1))
        with self.assertRaises(ValueError):
            _parse_cargos(event)

    def test_multiple_zero_delivery_ids_all_included(self):
        """Multiple DeliveryId=0 cargos are all kept and processed."""
        event = _make_event(
            _cargo("A", 100, delivery_id=0),
            _cargo("B", 200, delivery_id=0),
        )
        result = _parse_cargos(event)
        self.assertEqual(len(result), 2)


class GetCargoBonusTests(TestCase):
    def test_oak_log_zero_damage(self):
        # 0% damage → full bonus (100% of payment)
        self.assertEqual(get_cargo_bonus("Log_Oak_12ft", 8641, 0.0), 8641)

    def test_oak_log_full_damage(self):
        # 100% damage → no bonus
        self.assertEqual(get_cargo_bonus("Log_Oak_12ft", 8641, 1.0), 0)

    def test_oak_log_partial_damage(self):
        # 50% damage → 50% bonus
        self.assertEqual(get_cargo_bonus("Log_Oak_12ft", 10000, 0.5), 5000)

    def test_oak_log_quarter_damage(self):
        # 25% damage → 75% bonus
        self.assertEqual(get_cargo_bonus("Log_Oak_12ft", 10000, 0.25), 7500)

    def test_unknown_cargo_no_bonus(self):
        self.assertEqual(get_cargo_bonus("oranges", 10000, 0.0), 0)

    def test_unknown_cargo_with_damage(self):
        self.assertEqual(get_cargo_bonus("apples", 10000, 0.5), 0)


class ExtractDeliveryIdTests(TestCase):
    def test_extracts_positive_id(self):
        self.assertEqual(_extract_delivery_id({"Net_DeliveryId": 250235}), 250235)

    def test_zero_is_none(self):
        """DeliveryId 0 = non-job delivery, never a dedupe key."""
        self.assertIsNone(_extract_delivery_id({"Net_DeliveryId": 0}))

    def test_negative_is_none(self):
        """DeliveryId -1 = no id (free-roam loops, reconnect-lost jobs)."""
        self.assertIsNone(_extract_delivery_id({"Net_DeliveryId": -1}))
        self.assertIsNone(_extract_delivery_id({"Net_DeliveryId": "-1"}))

    def test_absent_is_none(self):
        self.assertIsNone(_extract_delivery_id({}))

    def test_string_id_converted(self):
        self.assertEqual(_extract_delivery_id({"Net_DeliveryId": "250235"}), 250235)

    def test_garbage_is_none(self):
        self.assertIsNone(_extract_delivery_id({"Net_DeliveryId": "not-a-number"}))
        self.assertIsNone(_extract_delivery_id({"Net_DeliveryId": None}))


class DuplicateDeliverySuppressionTests(TestCase):
    """Async ORM inside asyncio.run uses its own DB connection (it cannot
    see rows created on the sync test connection), so each scenario creates
    its actors and prior rows via async ORM and cleans them up afterwards —
    otherwise rows autocommit outside the TestCase transaction and leak
    into later tests."""

    async def _make_actor(self, unique_id):
        player = await Player.objects.acreate(
            unique_id=unique_id,
            discord_user_id=None,
            discord_name=f"dedupe-{unique_id}",
        )
        character = await Character.objects.acreate(
            player=player,
            guid=f"dedupe-{unique_id}",
            name=f"dedupe-{unique_id}",
        )
        return player, character

    async def _seed_prior(self, now, player, character, delivery_id,
                          cargo_key="Coal", hours_ago=1.0, seconds_ago=None):
        await ServerCargoArrivedLog.objects.acreate(
            timestamp=(
                now - timedelta(seconds=seconds_ago)
                if seconds_ago is not None
                else now - timedelta(hours=hours_ago)
            ),
            player=player,
            character=character,
            cargo_key=cargo_key,
            payment=10_000,
            weight=0,
            damage=0.0,
            delivery_id=delivery_id,
            data={},
        )

    def _log(self, now, player, character, delivery_id, cargo_key="Coal"):
        return ServerCargoArrivedLog(
            timestamp=now,
            player=player,
            character=character,
            cargo_key=cargo_key,
            payment=10_000,
            weight=0,
            damage=0.0,
            delivery_id=delivery_id,
            data={},
        )

    def test_first_emission_is_fresh(self):
        """No prior emission — every log is fresh."""
        async def scenario():
            player, character = await self._make_actor(990001)
            now = timezone.now()
            try:
                return await _split_duplicate_deliveries(
                    [self._log(now, player, character, 250235),
                     self._log(now, player, character, 250236)],
                    character, now,
                )
            finally:
                await self._cleanup(character)

        fresh, dups = asyncio.run(scenario())
        self.assertEqual(len(fresh), 2)
        self.assertEqual(dups, [])

    def test_second_emission_in_same_batch_is_duplicate(self):
        """Two emissions of one delivery in a single event burst."""
        async def scenario():
            player, character = await self._make_actor(990002)
            now = timezone.now()
            try:
                return await _split_duplicate_deliveries(
                    [self._log(now, player, character, 250235),
                     self._log(now, player, character, 250235)],
                    character, now,
                )
            finally:
                await self._cleanup(character)

        fresh, dups = asyncio.run(scenario())
        self.assertEqual(len(fresh), 1)
        self.assertEqual(len(dups), 1)

    def test_reemission_after_prior_row_is_duplicate(self):
        """The 2026-09-09 vssm signature: burst after a first paid emission."""
        async def scenario():
            player, character = await self._make_actor(990003)
            now = timezone.now()
            try:
                await self._seed_prior(now, player, character, 250235)
                logs = [
                    self._log(now, player, character, 250235, "Coal"),
                    self._log(now, player, character, 250235, "Coal"),
                    self._log(now, player, character, 250235, "Iron"),
                    self._log(now, player, character, 250236, "Coal"),
                ]
                return await _split_duplicate_deliveries(logs, character, now)
            finally:
                await self._cleanup(character)

        fresh, dups = asyncio.run(scenario())
        self.assertEqual(len(fresh), 2)
        self.assertEqual(len(dups), 2)
        for log in dups:
            self.assertEqual(
                (log.delivery_id, log.cargo_key), (250235, "Coal")
            )

    def test_same_delivery_other_cargo_key_is_fresh(self):
        async def scenario():
            player, character = await self._make_actor(990004)
            now = timezone.now()
            try:
                await self._seed_prior(now, player, character, 250235)
                return await _split_duplicate_deliveries(
                    [self._log(now, player, character, 250235, "Iron")],
                    character, now,
                )
            finally:
                await self._cleanup(character)

        fresh, dups = asyncio.run(scenario())
        self.assertEqual(len(fresh), 1)
        self.assertEqual(dups, [])

    def test_same_delivery_other_character_is_fresh(self):
        async def scenario():
            player_a, character_a = await self._make_actor(990005)
            player_b, character_b = await self._make_actor(990006)
            now = timezone.now()
            try:
                await self._seed_prior(now, player_a, character_a, 250235)
                return await _split_duplicate_deliveries(
                    [self._log(now, player_b, character_b, 250235)],
                    character_b, now,
                )
            finally:
                await self._cleanup(character_a)
                await self._cleanup(character_b)

        fresh, dups = asyncio.run(scenario())
        self.assertEqual(len(fresh), 1)
        self.assertEqual(dups, [])

    def test_window_expiry_is_fresh(self):
        async def scenario():
            player, character = await self._make_actor(990007)
            now = timezone.now()
            try:
                await self._seed_prior(
                    now, player, character, 250235, hours_ago=25.0
                )
                return await _split_duplicate_deliveries(
                    [self._log(now, player, character, 250235)],
                    character, now,
                )
            finally:
                await self._cleanup(character)

        fresh, dups = asyncio.run(scenario())
        self.assertEqual(len(fresh), 1)
        self.assertEqual(dups, [])

    def test_idless_identical_items_in_one_event_are_fresh(self):
        """Identical id-less items inside ONE event are genuine multi-item
        cargo (webhook aggregation contract) — all pay. Re-emission bursts
        arrive as separate events and are caught by the DB-prior lane."""
        async def scenario():
            player, character = await self._make_actor(990008)
            now = timezone.now()
            try:
                return await _split_duplicate_deliveries(
                    [self._log(now, player, character, None),
                     self._log(now, player, character, None),
                     self._log(now, player, character, None)],
                    character, now,
                )
            finally:
                await self._cleanup(character)

        fresh, dups = asyncio.run(scenario())
        self.assertEqual(len(fresh), 3)
        self.assertEqual(dups, [])

    def test_idless_different_money_is_fresh(self):
        """Id-less cargo with different payment/weight = different physical
        cargo (multi-item event); every item pays."""
        async def scenario():
            player, character = await self._make_actor(990009)
            now = timezone.now()
            try:
                log_zero = self._log(now, player, character, None,
                                     cargo_key="TrashBag")
                log_zero.payment = 0
                log_zero.weight = -1
                log_pay = self._log(now, player, character, None,
                                    cargo_key="TrashBag")
                log_pay.payment = 2000
                return await _split_duplicate_deliveries(
                    [log_zero, log_pay], character, now,
                )
            finally:
                await self._cleanup(character)

        fresh, dups = asyncio.run(scenario())
        self.assertEqual(len(fresh), 2)
        self.assertEqual(dups, [])

    def test_idless_reemission_after_prior_row_is_duplicate(self):
        """Prior id-less row 10s ago with the same money fingerprint is a
        duplicate (the reconnect burst after a first paid emission)."""
        async def scenario():
            player, character = await self._make_actor(990010)
            now = timezone.now()
            try:
                await self._seed_prior(
                    now, player, character, None, seconds_ago=10
                )
                return await _split_duplicate_deliveries(
                    [self._log(now, player, character, None)],
                    character, now,
                )
            finally:
                await self._cleanup(character)

        fresh, dups = asyncio.run(scenario())
        self.assertEqual(len(fresh), 0)
        self.assertEqual(len(dups), 1)

    def test_idless_window_expiry_is_fresh(self):
        """Legit repeat delivery: same cargo, same money, >60s later."""
        async def scenario():
            player, character = await self._make_actor(990011)
            now = timezone.now()
            try:
                await self._seed_prior(
                    now, player, character, None, seconds_ago=90
                )
                return await _split_duplicate_deliveries(
                    [self._log(now, player, character, None)],
                    character, now,
                )
            finally:
                await self._cleanup(character)

        fresh, dups = asyncio.run(scenario())
        self.assertEqual(len(fresh), 1)
        self.assertEqual(dups, [])

    def test_idless_other_character_is_fresh(self):
        """Money fingerprints are per-character."""
        async def scenario():
            player_a, character_a = await self._make_actor(990012)
            player_b, character_b = await self._make_actor(990013)
            now = timezone.now()
            try:
                await self._seed_prior(
                    now, player_a, character_a, None, seconds_ago=5
                )
                return await _split_duplicate_deliveries(
                    [self._log(now, player_b, character_b, None)],
                    character_b, now,
                )
            finally:
                await self._cleanup(character_a)
                await self._cleanup(character_b)

        fresh, dups = asyncio.run(scenario())
        self.assertEqual(len(fresh), 1)
        self.assertEqual(dups, [])

    def test_null_character_skips_dedupe(self):
        async def scenario():
            player, character = await self._make_actor(990008)
            now = timezone.now()
            try:
                await self._seed_prior(now, player, character, 250235)
                return await _split_duplicate_deliveries(
                    [self._log(now, player, character, 250235)], None, now
                )
            finally:
                await self._cleanup(character)

        fresh, dups = asyncio.run(scenario())
        self.assertEqual(len(fresh), 1)
        self.assertEqual(dups, [])

    async def _cleanup(self, character):
        await ServerCargoArrivedLog.objects.filter(character=character).adelete()
        await Character.objects.filter(pk=character.pk).adelete()
        await Player.objects.filter(unique_id=character.player_id).adelete()
