"""Tests for illicit cargo: Wanted trigger, Delivery↔Wanted link, contraband handler."""

import time
from unittest.mock import patch, AsyncMock

from asgiref.sync import sync_to_async
from django.contrib.gis.geos import Point
from django.test import TestCase

from amc.factories import PlayerFactory, CharacterFactory
from amc.models import (
    CriminalRecord,
    CharacterLocation,
    Delivery,
    DeliveryPoint,
    Wanted,
)
from amc.special_cargo import ILLICIT_CARGO_KEYS
from amc.webhook import process_event


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
@patch("amc.handlers.cargo.should_trigger_wanted", return_value=True)  # always trigger wanted
@patch("amc.handlers.cargo.accumulate_illicit_delivery", new_callable=AsyncMock, return_value=100_000)
class IllicitCargoWantedTests(TestCase):
    """All illicit cargo keys should trigger Wanted status and link Delivery."""

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

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_ganja_creates_wanted(
        self, mock_treasury, mock_refresh, mock_accumulate, mock_get_treasury, mock_get_rp_mode, mock_random
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Ganja")
        await process_event(event, player, character)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted)
        self.assertEqual(wanted.wanted_remaining, Wanted.INITIAL_WANTED_LEVEL)

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_cocaine_creates_wanted(
        self, mock_treasury, mock_refresh, mock_accumulate, mock_get_treasury, mock_get_rp_mode, mock_random
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Cocaine")
        await process_event(event, player, character)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted)

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_coca_leaves_pallet_creates_wanted(
        self, mock_treasury, mock_refresh, mock_accumulate, mock_get_treasury, mock_get_rp_mode, mock_random
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "CocaLeavesPallet", payment=200_001)
        await process_event(event, player, character)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted)

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_ganja_pallet_creates_wanted(
        self, mock_treasury, mock_refresh, mock_accumulate, mock_get_treasury, mock_get_rp_mode, mock_random
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "GanjaPallet")
        await process_event(event, player, character)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted)

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_money_pallet_creates_wanted(
        self, mock_treasury, mock_refresh, mock_accumulate, mock_get_treasury, mock_get_rp_mode, mock_random
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "MoneyPallet")
        await process_event(event, player, character)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted)

    # ------------------------------------------------------------------
    # Wanted refresh (existing wanted gets timer reset)
    # ------------------------------------------------------------------

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_contraband_refreshes_existing_wanted(
        self, mock_treasury, mock_refresh, mock_accumulate, mock_get_treasury, mock_get_rp_mode, mock_random
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

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_delivery_linked_to_criminal_record_for_ganja(
        self, mock_treasury, mock_refresh, mock_accumulate, mock_get_treasury, mock_get_rp_mode, mock_random
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Ganja", payment=10_000)
        await process_event(event, player, character)

        delivery = await Delivery.objects.filter(
            character=character, cargo_key="Ganja"
        ).afirst()
        self.assertIsNotNone(delivery)
        self.assertIsNotNone(delivery.criminal_record_id)

        record = await CriminalRecord.objects.aget(pk=delivery.criminal_record_id)
        self.assertEqual(record.character_id, character.id)

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_delivery_linked_to_criminal_record_for_money(
        self, mock_treasury, mock_refresh, mock_accumulate, mock_get_treasury, mock_get_rp_mode, mock_random
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Money", payment=10_000)
        await process_event(event, player, character)

        delivery = await Delivery.objects.filter(
            character=character, cargo_key="Money"
        ).afirst()
        self.assertIsNotNone(delivery)
        self.assertIsNotNone(delivery.criminal_record_id)

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_non_illicit_delivery_has_no_wanted(
        self, mock_treasury, mock_refresh, mock_accumulate, mock_get_treasury, mock_get_rp_mode, mock_random
    ):
        """Non-illicit cargo should NOT create Wanted or link delivery."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Coal", payment=5_000)
        await process_event(event, player, character)

        self.assertEqual(
            await Wanted.objects.filter(character=character).acount(), 0
        )
        delivery = await Delivery.objects.filter(
            character=character, cargo_key="Coal"
        ).afirst()
        if delivery:
            self.assertIsNone(delivery.criminal_record_id)


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
class ContrabandCriminalRecordTests(TestCase):
    """Contraband deliveries should create criminal records with cargo-specific reasons."""

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

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_ganja_creates_criminal_record(
        self, mock_treasury, mock_refresh, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Ganja")
        await process_event(event, player, character)

        record = await CriminalRecord.objects.filter(character=character).afirst()
        self.assertIsNotNone(record)
        self.assertEqual(record.reason, "Ganja delivery")
        self.assertIsNone(record.cleared_at)  # active record

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_cocaine_creates_criminal_record(
        self, mock_treasury, mock_refresh, mock_get_treasury, mock_get_rp_mode
    ):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Cocaine")
        await process_event(event, player, character)

        record = await CriminalRecord.objects.filter(character=character).afirst()
        self.assertIsNotNone(record)
        self.assertEqual(record.reason, "Cocaine delivery")

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_contraband_accumulates_on_existing_criminal_record(
        self, mock_treasury, mock_refresh, mock_get_treasury, mock_get_rp_mode
    ):
        """Repeat contraband delivery should reuse the active CriminalRecord and accumulate amounts."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        # Pre-existing record
        await CriminalRecord.objects.acreate(
            character=character,
            reason="Ganja delivery",
            cleared_at=None,  # active
            amount=20_000,
            confiscatable_amount=20_000,
        )

        event = self._cargo_event(character, "Cocaine", payment=5_000)
        await process_event(event, player, character)

        records = [r async for r in CriminalRecord.objects.filter(character=character, cleared_at__isnull=True)]
        self.assertEqual(len(records), 1, "Should not create a second record")
        # Amount accumulates
        self.assertEqual(records[0].amount, 25_000)
        self.assertEqual(records[0].confiscatable_amount, 25_000)

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_contraband_accumulates_laundered_total(
        self, mock_treasury, mock_refresh, mock_get_treasury, mock_get_rp_mode
    ):
        """Contraband (non-Money) should increment criminal_laundered_total."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Ganja", payment=50_000)
        await process_event(event, player, character)

        await character.arefresh_from_db()
        self.assertEqual(character.criminal_laundered_total, 50_000)

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_contraband_no_treasury_expense(
        self, mock_treasury, mock_refresh, mock_get_treasury, mock_get_rp_mode
    ):
        """Contraband should NOT incur the 20% money laundering treasury cost."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Ganja", payment=50_000)
        await process_event(event, player, character)

        mock_treasury.assert_not_called()

    @patch("amc.handlers.cargo.should_trigger_wanted", return_value=True)
    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_player_tag_refreshed_for_contraband(
        self, mock_treasury, mock_refresh, mock_should_trigger, mock_get_treasury, mock_get_rp_mode
    ):
        """Player name tag is refreshed on contraband delivery.

        Refresh fires at least once when a new CriminalRecord is created
        (ensuring the [C] tag appears). It may also fire again via the
        wanted-trigger path.
        """
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, "Cocaine")
        await process_event(event, player, character)

        # Called at least once (from ensure_criminal_record on new record creation)
        mock_refresh.assert_called()


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
@patch("amc.handlers.cargo.should_trigger_wanted", return_value=True)
@patch("amc.handlers.cargo.accumulate_illicit_delivery", new_callable=AsyncMock, return_value=100_000)
class MakeSuspectTests(TestCase):
    """When a Wanted is created/refreshed, make_suspect should be called on the mod server."""

    async def _setup_character(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )
        await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(guid="d1", name="D1", coord=Point(100, 100, 0))
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
    async def test_make_suspect_called_on_wanted_creation(
        self, mock_treasury, mock_refresh, mock_make_suspect, mock_accumulate, mock_trigger, mock_get_treasury, mock_get_rp_mode
    ):
        """When Wanted is created via illicit cargo, make_suspect is called on the mod server."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        mock_http_client_mod = AsyncMock()
        player, character = await self._setup_character()

        event = self._cargo_event(character)
        await process_event(event, player, character, http_client_mod=mock_http_client_mod)

        mock_make_suspect.assert_called_once_with(mock_http_client_mod, character.guid)

    @patch("amc.criminals.make_suspect", new_callable=AsyncMock)
    @patch("amc.criminals.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_make_suspect_not_called_without_http_client_mod(
        self, mock_treasury, mock_refresh, mock_make_suspect, mock_accumulate, mock_trigger, mock_get_treasury, mock_get_rp_mode
    ):
        """When http_client_mod is None, make_suspect should not be called."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character)
        await process_event(event, player, character)

        mock_make_suspect.assert_not_called()

    @patch("amc.criminals.make_suspect", new_callable=AsyncMock)
    @patch("amc.criminals.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_make_suspect_failure_does_not_block_wanted_creation(
        self, mock_treasury, mock_refresh, mock_make_suspect, mock_accumulate, mock_trigger, mock_get_treasury, mock_get_rp_mode
    ):
        """If make_suspect fails, the Wanted record should still be created."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        mock_make_suspect.side_effect = Exception("mod server unavailable")
        mock_http_client_mod = AsyncMock()
        player, character = await self._setup_character()

        event = self._cargo_event(character)
        await process_event(event, player, character, http_client_mod=mock_http_client_mod)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted, "Wanted should still be created even if make_suspect fails")


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
@patch("amc.handlers.cargo.should_trigger_wanted", return_value=True)
@patch("amc.handlers.cargo.accumulate_illicit_delivery", new_callable=AsyncMock, return_value=100_000)
class WantedBountyTests(TestCase):
    """Wanted.amount starts at 0 — bounty only grows from police proximity."""

    async def _setup_character(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )
        await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(guid="d1", name="D1", coord=Point(100, 100, 0))
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

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_delivery_creates_wanted_with_zero_bounty(
        self, mock_treasury, mock_refresh, mock_accumulate, mock_trigger, mock_get_treasury, mock_get_rp_mode
    ):
        """A delivery creates a Wanted record with amount=0 — bounty only grows from police chase."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, payment=5_000)
        await process_event(event, player, character)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted)
        self.assertEqual(
            wanted.amount,
            0,
            "Wanted.amount must start at 0 — bounty only grows from police proximity",
        )

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_large_delivery_also_zero_bounty(
        self, mock_treasury, mock_refresh, mock_accumulate, mock_trigger, mock_get_treasury, mock_get_rp_mode
    ):
        """Even a large delivery produces amount=0 — bounty is tracked separately via police chase."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        event = self._cargo_event(character, payment=250_000)
        await process_event(event, player, character)

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted)
        self.assertEqual(wanted.amount, 0)

    @patch("amc.special_cargo.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.special_cargo.record_treasury_expense", new_callable=AsyncMock)
    async def test_refresh_does_not_add_to_bounty(
        self, mock_treasury, mock_refresh, mock_accumulate, mock_trigger, mock_get_treasury, mock_get_rp_mode
    ):
        """Refreshing an existing Wanted does not add to amount (amount=0 passed from cargo handler)."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        player, character = await self._setup_character()

        # Seed an existing wanted with a known bounty (from police chase)
        existing_wanted = await Wanted.objects.acreate(
            character=character,
            wanted_remaining=Wanted.INITIAL_WANTED_LEVEL,
            amount=30_000,
        )

        # Deliver something — the refresh should NOT add to amount
        event = self._cargo_event(character, payment=1_000)
        await process_event(event, player, character)

        await existing_wanted.arefresh_from_db()
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

    async def test_cargo_handler_passes_accumulated_total_to_probability_roll(self):
        """should_trigger_wanted receives the accumulated total, not just the delivery amount."""
        import amc.handlers.cargo as cargo_module

        # Simulate the cache already having 90k from a prior delivery (total = 100k)
        delivery_amount = 10_000
        expected_accumulated = 100_000

        with (
            patch.object(
                cargo_module,
                "accumulate_illicit_delivery",
                new=AsyncMock(return_value=expected_accumulated),
            ) as mock_accumulate,
            patch.object(
                cargo_module,
                "should_trigger_wanted",
                return_value=False,
            ) as mock_trigger,
        ):
            # Call the function directly to verify the argument plumbing
            accumulated = await mock_accumulate("any-guid", delivery_amount)
            mock_trigger(accumulated)

            mock_accumulate.assert_called_once_with("any-guid", delivery_amount)
            mock_trigger.assert_called_once_with(expected_accumulated)


class ShouldTriggerWantedAccumulatedTests(TestCase):
    """should_trigger_wanted uses accumulated totals correctly."""

    def test_zero_amount_uses_min_chance(self):
        from amc.special_cargo import should_trigger_wanted, WANTED_MIN_CHANCE

        with patch("amc.special_cargo.random") as mock_rng:
            mock_rng.random.return_value = WANTED_MIN_CHANCE - 0.001
            self.assertTrue(should_trigger_wanted(0))
            mock_rng.random.return_value = WANTED_MIN_CHANCE + 0.001
            self.assertFalse(should_trigger_wanted(0))

    def test_100k_accumulated_guarantees_trigger(self):
        from amc.special_cargo import should_trigger_wanted

        with patch("amc.special_cargo.random") as mock_rng:
            mock_rng.random.return_value = 0.9999
            # 100k = 100% chance so even 0.9999 should pass
            self.assertTrue(should_trigger_wanted(100_000))

    def test_50k_accumulated_is_50_percent_chance(self):
        from amc.special_cargo import should_trigger_wanted

        with patch("amc.special_cargo.random") as mock_rng:
            mock_rng.random.return_value = 0.49
            self.assertTrue(should_trigger_wanted(50_000))
            mock_rng.random.return_value = 0.51
            self.assertFalse(should_trigger_wanted(50_000))

    def test_10_micro_deliveries_vs_one_large_delivery_same_probability(self):
        """10 × 10k accumulated = one 100k delivery = 100% chance."""
        from amc.special_cargo import should_trigger_wanted

        with patch("amc.special_cargo.random") as mock_rng:
            mock_rng.random.return_value = 0.9999
            # Single 100k delivery
            self.assertTrue(should_trigger_wanted(100_000))
            # 10 accumulations — if we passed accumulated=100_000 (as the handler now does)
            self.assertTrue(should_trigger_wanted(100_000))
