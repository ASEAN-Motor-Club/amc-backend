"""Tests for illicit cargo: Wanted trigger, Delivery↔Wanted link, contraband handler."""

import time
from datetime import timedelta
from unittest.mock import patch, AsyncMock

from asgiref.sync import sync_to_async
from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone

from amc.factories import PlayerFactory, CharacterFactory
from amc.models import (
    CharacterLocation,
    DeliveryPoint,
    PendingWanted,
    Wanted,
)
from amc.special_cargo import ILLICIT_CARGO_KEYS
from amc.webhook import process_event


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
@patch("amc.handlers.cargo.accumulate_illicit_delivery", new_callable=AsyncMock, return_value=100_000)
class IllicitCargoWantedTests(TestCase):
    """All illicit cargo keys should link a criminal record and refresh Wanted status
    (the random auto-wanted trigger is deprecated — see b27c97d)."""

    async def _setup_character(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )
        await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="d1", name="D1", coord=Point(100, 100, 0)
        )
        return player, character

    def _cargo_event(self, character, cargo_key, payment=5000):
        return {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {
                        "Net_CargoKey": cargo_key,
                        "Net_Payment": payment,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }

    # ------------------------------------------------------------------
    # Wanted creation
    # ------------------------------------------------------------------

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_ganja_refreshes_existing_wanted(
        self, mock_treasury, mock_accumulate, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        # A not-yet-wanted delivery does NOT auto-create wanted (deprecated trigger);
        # assert there is no active Wanted after the delivery.
        event = self._cargo_event(character, "Ganja")
        await process_event(event, player, character)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNone(wanted)

        # Now seed a wanted — a subsequent illicit delivery refreshes (not duplicates) it.
        created = await Wanted.objects.acreate(
            character=character, wanted_remaining=Wanted.INITIAL_WANTED_LEVEL
        )
        await process_event(event, player, character)
        await created.arefresh_from_db()
        self.assertEqual(created.wanted_remaining, Wanted.INITIAL_WANTED_LEVEL)

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_cocaine_does_not_auto_create_wanted(
        self, mock_treasury, mock_accumulate, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Cocaine")
        await process_event(event, player, character)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNone(wanted)

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_coca_leaves_pallet_does_not_auto_create_wanted(
        self, mock_treasury, mock_accumulate, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "CocaLeavesPallet", payment=200_001)
        await process_event(event, player, character)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNone(wanted)

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_ganja_pallet_does_not_auto_create_wanted(
        self, mock_treasury, mock_accumulate, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "GanjaPallet")
        await process_event(event, player, character)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNone(wanted)

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_money_pallet_does_not_auto_create_wanted(
        self, mock_treasury, mock_accumulate, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "MoneyPallet")
        await process_event(event, player, character)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNone(wanted)

    # ------------------------------------------------------------------
    # Wanted refresh (existing wanted gets timer reset)
    # ------------------------------------------------------------------

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_contraband_refreshes_existing_wanted(
        self, mock_treasury, mock_accumulate, mock_get_treasury, mock_get_rp_mode
    ):
        """If already wanted, a new contraband delivery resets the countdown."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        # Pre-existing wanted with 60 seconds remaining
        await Wanted.objects.acreate(
            character=character,
            wanted_remaining=60,
        )

        event = self._cargo_event(character, "Ganja")
        await process_event(event, player, character)

        wanted_records = [
            w
            async for w in Wanted.objects.filter(
                character=character, expired_at__isnull=True
            )
        ]
        self.assertEqual(len(wanted_records), 1, "Should not create a second wanted")
        self.assertEqual(
            wanted_records[0].wanted_remaining, Wanted.INITIAL_WANTED_LEVEL
        )

    # ------------------------------------------------------------------
    # Delivery → Wanted FK link
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Score accumulation
    # ------------------------------------------------------------------

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_non_illicit_delivery_has_no_wanted(
        self, mock_treasury, mock_accumulate, mock_get_treasury, mock_get_rp_mode
    ):
        """Non-illicit cargo should NOT create a Wanted."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Coal", payment=5_000)
        await process_event(event, player, character)

        self.assertEqual(
            await Wanted.objects.filter(character=character).acount(), 0
        )


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
class ContrabandScoreTests(TestCase):
    """Contraband deliveries should accumulate the criminal score (rap sheet)."""

    async def _setup_character(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )
        await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="d1", name="D1", coord=Point(100, 100, 0)
        )
        return player, character

    def _cargo_event(self, character, cargo_key, payment=5000):
        return {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {
                        "Net_CargoKey": cargo_key,
                        "Net_Payment": payment,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_ganja_accumulates_criminal_score(
        self, mock_treasury, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Ganja")
        await process_event(event, player, character)

        await character.arefresh_from_db(fields=["criminal_score"])
        self.assertEqual(character.criminal_score, 5_000)

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_cocaine_accumulates_criminal_score(
        self, mock_treasury, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Cocaine")
        await process_event(event, player, character)

        await character.arefresh_from_db(fields=["criminal_score"])
        self.assertEqual(character.criminal_score, 5_000)

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_contraband_accumulates_on_existing_criminal_score(
        self, mock_treasury, mock_get_treasury, mock_get_rp_mode
    ):
        """Repeat contraband delivery accumulates onto the existing criminal score."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        character.criminal_score = 20_000
        await character.asave(update_fields=["criminal_score"])

        event = self._cargo_event(character, "Cocaine", payment=5_000)
        await process_event(event, player, character)

        await character.arefresh_from_db(fields=["criminal_score"])
        self.assertEqual(character.criminal_score, 25_000)

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_contraband_accumulates_score(
        self, mock_treasury, mock_get_treasury, mock_get_rp_mode
    ):
        """Contraband (non-Money) should increment criminal_score."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Ganja", payment=50_000)
        await process_event(event, player, character)

        await character.arefresh_from_db(fields=["criminal_score"])
        self.assertEqual(character.criminal_score, 50_000)

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_contraband_no_treasury_expense(
        self, mock_treasury, mock_get_treasury, mock_get_rp_mode
    ):
        """Contraband should NOT incur the 20% money laundering treasury cost."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Ganja", payment=50_000)
        await process_event(event, player, character)

        mock_treasury.assert_not_called()


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
@patch("amc.handlers.cargo.accumulate_illicit_delivery", new_callable=AsyncMock, return_value=100_000)
class MakeSuspectTests(TestCase):
    """When a Wanted is refreshed via contraband, make_suspect should be called on the mod server.

    The random auto-wanted trigger is deprecated (b27c97d), so these tests seed an
    existing Wanted record — an illicit delivery then refreshes it, exercising the
    make_suspect call in create_or_refresh_wanted.
    """

    async def _setup_character(self, seed_wanted=False):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )
        await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(guid="d1", name="D1", coord=Point(100, 100, 0))
        if seed_wanted:
            await Wanted.objects.acreate(
                character=character,
                wanted_remaining=Wanted.INITIAL_WANTED_LEVEL,
            )
        return player, character

    def _cargo_event(self, character, cargo_key="Ganja", payment=5_000):
        return {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {
                        "Net_CargoKey": cargo_key,
                        "Net_Payment": payment,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }

    @patch("amc.criminals.make_suspect", new_callable=AsyncMock)
    @patch("amc.criminals.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_make_suspect_called_on_wanted_refresh(
        self, mock_treasury, mock_refresh, mock_make_suspect, mock_accumulate, mock_get_treasury, mock_get_rp_mode
    ):
        """When Wanted is refreshed via contraband, make_suspect is called on the mod server."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        mock_http_client_mod = AsyncMock()
        player, character = await self._setup_character(seed_wanted=True)

        event = self._cargo_event(character)
        await process_event(event, player, character, http_client_mod=mock_http_client_mod)

        mock_make_suspect.assert_called_once_with(mock_http_client_mod, character.guid)

    @patch("amc.criminals.make_suspect", new_callable=AsyncMock)
    @patch("amc.criminals.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_make_suspect_not_called_without_http_client_mod(
        self, mock_treasury, mock_refresh, mock_make_suspect, mock_accumulate, mock_get_treasury, mock_get_rp_mode
    ):
        """When http_client_mod is None, make_suspect should not be called."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character(seed_wanted=True)

        event = self._cargo_event(character)
        await process_event(event, player, character)

        mock_make_suspect.assert_not_called()

    @patch("amc.criminals.make_suspect", new_callable=AsyncMock)
    @patch("amc.criminals.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_make_suspect_failure_does_not_block_wanted_refresh(
        self, mock_treasury, mock_refresh, mock_make_suspect, mock_accumulate, mock_get_treasury, mock_get_rp_mode
    ):
        """If make_suspect fails, the Wanted record should still be refreshed."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        mock_make_suspect.side_effect = Exception("mod server unavailable")
        mock_http_client_mod = AsyncMock()
        player, character = await self._setup_character(seed_wanted=True)

        event = self._cargo_event(character)
        await process_event(event, player, character, http_client_mod=mock_http_client_mod)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted, "Wanted should still be refreshed even if make_suspect fails")


class IllicitCargoKeysRegistryTests(TestCase):
    """Verify that the ILLICIT_CARGO_KEYS set and handler registry are consistent."""

    def test_illicit_cargo_keys_contains_all_expected(self):
        expected = {"Money", "Ganja", "CocaLeavesPallet", "GanjaPallet", "Cocaine", "MoneyPallet", "Moonshine", "CocaPaste", "CocaineBricks"}
        self.assertEqual(ILLICIT_CARGO_KEYS, expected)

    def test_all_illicit_keys_have_handlers(self):
        from amc.special_cargo import SPECIAL_CARGO_HANDLERS

        for key in ILLICIT_CARGO_KEYS:
            self.assertIn(key, SPECIAL_CARGO_HANDLERS, f"{key} missing from handler registry")

    def test_no_non_illicit_keys_in_handlers(self):
        from amc.special_cargo import SPECIAL_CARGO_HANDLERS

        for key in SPECIAL_CARGO_HANDLERS:
            self.assertIn(key, ILLICIT_CARGO_KEYS, f"{key} in handlers but not in ILLICIT_CARGO_KEYS")


# ---------------------------------------------------------------------------
# WantedBountyTests — minimum bounty enforcement
# ---------------------------------------------------------------------------


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
@patch("amc.handlers.cargo.accumulate_illicit_delivery", new_callable=AsyncMock, return_value=100_000)
class WantedBountyTests(TestCase):
    """Wanted.amount stays at seed — bounty only grows from police proximity."""

    async def _setup_character(self, seed_bounty=0):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )
        await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(guid="d1", name="D1", coord=Point(100, 100, 0))
        if seed_bounty is not None:
            await Wanted.objects.acreate(
                character=character,
                wanted_remaining=Wanted.INITIAL_WANTED_LEVEL,
                amount=seed_bounty,
            )
        return player, character

    def _cargo_event(self, character, cargo_key="Ganja", payment=5_000):
        return {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {
                        "Net_CargoKey": cargo_key,
                        "Net_Payment": payment,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_refresh_keeps_zero_bounty(
        self, mock_treasury, mock_accumulate, mock_get_treasury, mock_get_rp_mode
    ):
        """An illicit delivery to an already-wanted player (bounty=0 seed) does not add to amount —
        bounty only grows from police proximity."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character(seed_bounty=0)

        # Deliver something — the refresh should NOT add to amount
        event = self._cargo_event(character, payment=5_000)
        await process_event(event, player, character)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted)
        self.assertEqual(wanted.amount, 0, "Delivery must not add to Wanted bounty")

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_large_delivery_also_keeps_zero_bounty(
        self, mock_treasury, mock_accumulate, mock_get_treasury, mock_get_rp_mode
    ):
        """Even a large delivery to an already-wanted player produces amount=0 —
        bounty is tracked separately via police chase."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character(seed_bounty=0)

        event = self._cargo_event(character, payment=250_000)
        await process_event(event, player, character)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted)
        self.assertEqual(wanted.amount, 0)

    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_refresh_does_not_add_to_bounty(
        self, mock_treasury, mock_accumulate, mock_get_treasury, mock_get_rp_mode
    ):
        """Refreshing an existing Wanted does not add to amount (amount=0 passed from cargo handler)."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character(seed_bounty=30_000)

        # Deliver something — the refresh should NOT add to amount
        event = self._cargo_event(character, payment=1_000)
        await process_event(event, player, character)

        existing_wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        # 30k (from chase) + 0 (cargo handler passes amount=0) = 30k
        self.assertEqual(existing_wanted.amount, 30_000)


# ---------------------------------------------------------------------------
# DeliveryDebounceAccumulationTests — cargo splitting protection
# ---------------------------------------------------------------------------


class DeliveryDebounceAccumulationTests(TestCase):
    """accumulate_illicit_delivery sums amounts within the debounce window."""

    async def test_first_delivery_returns_its_own_amount(self):
        """With an empty cache the returned total equals the delivery amount."""
        from amc.special_cargo import accumulate_illicit_delivery
        from django.core.cache import cache

        guid = "test-guid-001"
        await cache.adelete(f"illicit_delivery_total:{guid}")
        total = await accumulate_illicit_delivery(guid, 10_000)
        self.assertEqual(total, 10_000)

    async def test_subsequent_deliveries_accumulate(self):
        """A second delivery within the window is added to the running total."""
        from amc.special_cargo import accumulate_illicit_delivery
        from django.core.cache import cache

        guid = "test-guid-002"
        await cache.adelete(f"illicit_delivery_total:{guid}")
        await accumulate_illicit_delivery(guid, 10_000)
        total = await accumulate_illicit_delivery(guid, 8_000)
        self.assertEqual(total, 18_000)

    async def test_ten_micro_deliveries_accumulate_to_full_amount(self):
        """10 × 10k deliveries accumulate to 100k — the full-chance threshold."""
        from amc.special_cargo import accumulate_illicit_delivery
        from django.core.cache import cache

        guid = "test-guid-003"
        await cache.adelete(f"illicit_delivery_total:{guid}")
        total = 0
        for _ in range(10):
            total = await accumulate_illicit_delivery(guid, 10_000)
        self.assertEqual(total, 100_000)


class WantedTriggerChanceTests(TestCase):
    """wanted_trigger_chance: ratio curve + cop attenuation (freeman 2026-09-20).

    Anchors are the KB values (YouTrack 183-4): fresh records roll against the
    100k yardstick floor; established criminals roll colder for the same haul.
    """

    def test_fresh_record_anchors(self):
        from amc.special_cargo import wanted_trigger_chance

        for pay, expected in (
            (10_000, 0.095),
            (50_000, 0.381),
            (100_000, 0.463),
            (500_000, 0.498),
            (1_500_000, 0.500),
        ):
            self.assertAlmostEqual(
                wanted_trigger_chance(pay, 0, None), expected, places=3, msg=f"pay={pay}"
            )

    def test_one_million_score_anchors(self):
        from amc.special_cargo import wanted_trigger_chance

        for pay, expected in (
            (100_000, 0.167),
            (500_000, 0.454),
            (1_000_000, 0.488),
            (1_500_000, 0.494),
        ):
            self.assertAlmostEqual(
                wanted_trigger_chance(pay, 1_000_000, None),
                expected,
                places=3,
                msg=f"pay={pay}",
            )

    def test_kingpin_20m_anchors(self):
        from amc.special_cargo import wanted_trigger_chance

        for pay, expected in (
            (500_000, 0.090),
            (1_000_000, 0.177),
            (1_500_000, 0.261),
        ):
            self.assertAlmostEqual(
                wanted_trigger_chance(pay, 20_000_000, None),
                expected,
                places=3,
                msg=f"pay={pay}",
            )

    def test_stays_within_floor_and_ceiling(self):
        from amc.special_cargo import (
            WANTED_TRIGGER_CEILING_CHANCE,
            WANTED_TRIGGER_FLOOR_CHANCE,
            wanted_trigger_chance,
        )

        for score in (0, 100_000, 1_000_000, 20_000_000, 100_000_000):
            for pay in (0, 1_000, 10_000, 50_000, 100_000, 500_000, 1_500_000, 10_000_000):
                chance = wanted_trigger_chance(pay, score, None)
                self.assertGreaterEqual(chance, WANTED_TRIGGER_FLOOR_CHANCE)
                self.assertLessEqual(chance, WANTED_TRIGGER_CEILING_CHANCE)

    def test_monotone_in_pay_and_score(self):
        from amc.special_cargo import wanted_trigger_chance

        last = 0.0
        for pay in (1_000, 10_000, 100_000, 500_000, 1_500_000):
            chance = wanted_trigger_chance(pay, 0, None)
            self.assertGreaterEqual(chance, last)
            last = chance
        last = 1.0
        for score in (0, 100_000, 1_000_000, 20_000_000):
            chance = wanted_trigger_chance(100_000, score, None)
            self.assertLessEqual(chance, last)
            last = chance

    def test_attenuation_profile(self):
        """γ=2 ramp on a fresh 100k run (base 46.3%)."""
        from amc.special_cargo import wanted_trigger_chance

        for metres, expected in (
            (0, 0.050),
            (250, 0.076),
            (500, 0.153),
            (750, 0.282),
            (1_000, 0.463),
            (2_000, 0.463),
        ):
            self.assertAlmostEqual(
                wanted_trigger_chance(100_000, 0, metres),
                expected,
                places=3,
                msg=f"distance={metres}",
            )


class CopAttenuationMultiplierTests(TestCase):
    """cop_attenuation_multiplier: quadratic ramp, clamped, fail-open on None."""

    def test_profile(self):
        from amc.special_cargo import cop_attenuation_multiplier

        self.assertEqual(cop_attenuation_multiplier(0), 0.0)
        self.assertAlmostEqual(cop_attenuation_multiplier(250), 0.0625, places=9)
        self.assertAlmostEqual(cop_attenuation_multiplier(500), 0.25, places=9)
        self.assertAlmostEqual(cop_attenuation_multiplier(750), 0.5625, places=9)
        self.assertEqual(cop_attenuation_multiplier(1_000), 1.0)
        self.assertEqual(cop_attenuation_multiplier(10_000), 1.0)

    def test_none_fails_open(self):
        from amc.special_cargo import cop_attenuation_multiplier

        self.assertEqual(cop_attenuation_multiplier(None), 1.0)

    def test_negative_distance_clamps_to_zero(self):
        from amc.special_cargo import cop_attenuation_multiplier

        self.assertEqual(cop_attenuation_multiplier(-100), 0.0)


class ShouldTriggerWantedRollTests(TestCase):
    """should_trigger_wanted: compares random() against the computed chance."""

    def test_roll_boundary_at_chance(self):
        from amc.special_cargo import should_trigger_wanted

        with patch("amc.special_cargo.random") as mock_rng:
            # Fresh 10k haul → chance exactly 9.5%
            mock_rng.random.return_value = 0.0949
            self.assertTrue(should_trigger_wanted(10_000, 0, None))
            mock_rng.random.return_value = 0.0951
            self.assertFalse(should_trigger_wanted(10_000, 0, None))

    def test_point_blank_chance_is_the_floor(self):
        """Point-blank under a cop the sweep is dead — only the 5% floor rolls."""
        from amc.special_cargo import (
            WANTED_TRIGGER_FLOOR_CHANCE,
            should_trigger_wanted,
            wanted_trigger_chance,
        )

        self.assertEqual(
            wanted_trigger_chance(50_000, 20_000_000, 0), WANTED_TRIGGER_FLOOR_CHANCE
        )
        with patch("amc.special_cargo.random") as mock_rng:
            mock_rng.random.return_value = WANTED_TRIGGER_FLOOR_CHANCE - 0.001
            self.assertTrue(should_trigger_wanted(50_000, 20_000_000, 0))
            mock_rng.random.return_value = WANTED_TRIGGER_FLOOR_CHANCE + 0.001
            self.assertFalse(should_trigger_wanted(50_000, 20_000_000, 0))

    def test_never_guarantees_trigger(self):
        """No haul size reaches 100% — the ceiling is asymptotic."""
        from amc.special_cargo import should_trigger_wanted

        with patch("amc.special_cargo.random") as mock_rng:
            mock_rng.random.return_value = 0.9999
            self.assertFalse(should_trigger_wanted(10_000_000, 0, 5_000))


@patch("amc.criminals.send_system_message", new_callable=AsyncMock)
@patch("amc.criminals.refresh_player_name", new_callable=AsyncMock)
@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
@patch("amc.handlers.cargo.nearest_effective_cop_distance_m", new_callable=AsyncMock)
@patch(
    "amc.handlers.cargo.accumulate_illicit_delivery",
    new_callable=AsyncMock,
    return_value=100_000,
)
class WantedTriggerRestoreTests(TestCase):
    """Handler-level tests for the restored ratio-driven trigger.

    Dormant rule: zero effective cops → no roll at all. Cop presence +
    distance are mocked at the import site (amc.handlers.cargo); the roll is
    controlled via amc.special_cargo.random.
    """

    async def _setup_character(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )
        await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="d1", name="D1", coord=Point(100, 100, 0)
        )
        return player, character

    def _cargo_event(self, character, cargo_key, payment=5000):
        return {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "Cargos": [
                    {
                        "Net_CargoKey": cargo_key,
                        "Net_Payment": payment,
                        "Net_Weight": 10.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }

    async def test_dormant_system_never_triggers(
        self, mock_accumulate, mock_cops, mock_get_treasury, mock_get_rp_mode,
        mock_send_system, mock_refresh_crim,
    ):
        """Zero effective cops → no roll, no Wanted (dormant rule)."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        mock_cops.return_value = (False, None)
        player, character = await self._setup_character()

        with patch("amc.special_cargo.random") as mock_rng:
            mock_rng.random.return_value = 0.0  # would pass any chance > 0
            event = self._cargo_event(character, "Ganja", payment=500_000)
            await process_event(event, player, character)

        exists = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).aexists()
        self.assertFalse(exists)
        pending_exists = await PendingWanted.objects.filter(
            character=character
        ).aexists()
        self.assertFalse(pending_exists)
        mock_cops.assert_awaited_once()

    async def test_roll_hit_schedules_pending_wanted(
        self, mock_accumulate, mock_cops, mock_get_treasury, mock_get_rp_mode,
        mock_send_system, mock_refresh_crim,
    ):
        """Grace period (freeman 2026-09-20): a rolled trigger does not create
        the Wanted — it schedules a PendingWanted and warns the criminal."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        mock_cops.return_value = (True, None)
        player, character = await self._setup_character()

        with patch("amc.special_cargo.random") as mock_rng:
            mock_rng.random.return_value = 0.0
            event = self._cargo_event(character, "Ganja", payment=5000)
            await process_event(event, player, character)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNone(wanted)
        pending = await PendingWanted.objects.filter(character=character).afirst()
        self.assertIsNotNone(pending)
        self.assertEqual(pending.trigger_amount, 100_000)

    async def test_roll_miss_does_not_create_wanted(
        self, mock_accumulate, mock_cops, mock_get_treasury, mock_get_rp_mode,
        mock_send_system, mock_refresh_crim,
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        mock_cops.return_value = (True, None)
        player, character = await self._setup_character()

        with patch("amc.special_cargo.random") as mock_rng:
            mock_rng.random.return_value = 0.999
            event = self._cargo_event(character, "Ganja", payment=5000)
            await process_event(event, player, character)

        exists = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).aexists()
        self.assertFalse(exists)
        pending_exists = await PendingWanted.objects.filter(
            character=character
        ).aexists()
        self.assertFalse(pending_exists)

    async def test_pending_suppresses_second_roll(
        self, mock_accumulate, mock_cops, mock_get_treasury, mock_get_rp_mode,
        mock_send_system, mock_refresh_crim,
    ):
        """A delivery landing inside an open grace window neither rolls again
        nor schedules a second pending trigger."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()
        pending = await PendingWanted.objects.acreate(
            character=character,
            apply_at=timezone.now() + timedelta(seconds=20),
            trigger_amount=100_000,
        )

        with patch("amc.special_cargo.random") as mock_rng:
            mock_rng.random.return_value = 0.0  # would pass any chance
            event = self._cargo_event(character, "Ganja", payment=5000)
            await process_event(event, player, character)

        mock_cops.assert_not_awaited()  # no roll — pending counts as wanted
        count = await PendingWanted.objects.filter(character=character).acount()
        self.assertEqual(count, 1)
        await pending.arefresh_from_db()  # untouched — same window continues

    async def test_roll_receives_debounce_total_pre_accrual_score_and_distance(
        self, mock_accumulate, mock_cops, mock_get_treasury, mock_get_rp_mode,
        mock_send_system, mock_refresh_crim,
    ):
        """The roll sees the debounce aggregate, the PRE-accrual lifetime
        total, and the cop distance from the gating helper."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        mock_cops.return_value = (True, 350.0)
        mock_accumulate.return_value = 5_000
        player, character = await self._setup_character()
        character.criminal_score = 1_000_000
        await character.asave(update_fields=["criminal_score"])

        with patch("amc.handlers.cargo.should_trigger_wanted") as mock_roll:
            mock_roll.return_value = False
            event = self._cargo_event(character, "Ganja", payment=5_000)
            await process_event(event, player, character)

        mock_roll.assert_called_once_with(5_000, 1_000_000, 350.0)

    async def test_already_wanted_refreshes_without_roll(
        self, mock_accumulate, mock_cops, mock_get_treasury, mock_get_rp_mode,
        mock_send_system, mock_refresh_crim,
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        mock_cops.return_value = (True, None)
        player, character = await self._setup_character()
        seeded = await Wanted.objects.acreate(
            character=character, wanted_remaining=300
        )

        with patch("amc.handlers.cargo.should_trigger_wanted") as mock_roll:
            event = self._cargo_event(character, "Ganja", payment=5000)
            await process_event(event, player, character)

        mock_roll.assert_not_called()
        mock_cops.assert_not_awaited()
        await seeded.arefresh_from_db()
        self.assertEqual(seeded.wanted_remaining, Wanted.INITIAL_WANTED_LEVEL)
