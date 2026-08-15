import time
from decimal import Decimal
from unittest.mock import patch, AsyncMock
from django.test import TestCase
from django.contrib.gis.geos import Point
from asgiref.sync import sync_to_async
from amc.factories import PlayerFactory, CharacterFactory
from amc.webhook import process_event
from amc.models import (
    DeliveryPoint,
    CharacterLocation,
    CriminalRecord,
)
from amc_finance.services import get_treasury_fund_balance


def _money_cargo_event(character_guid, player_id, payment=10_000):
    return {
        "hook": "ServerCargoArrived",
        "timestamp": int(time.time()),
        "data": {
            "CharacterGuid": str(character_guid),
            "PlayerId": str(player_id),
            "Cargos": [
                {
                    "Net_CargoKey": "Money",
                    "Net_Payment": payment,
                    "Net_Weight": 50.0,
                    "Net_Damage": 0.0,
                    "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                    "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                }
            ],
        },
    }


async def _setup_character(guid_suffix=""):
    """Create player + character + required related objects."""
    player = await sync_to_async(PlayerFactory)()
    guid = f"money-test-{guid_suffix or player.unique_id}"
    character = await sync_to_async(CharacterFactory)(player=player, guid=guid)
    await CharacterLocation.objects.acreate(
        character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
    )
    await DeliveryPoint.objects.aget_or_create(
        guid="ms1", defaults={"name": "MS1", "coord": Point(0, 0, 0)}
    )
    await DeliveryPoint.objects.aget_or_create(
        guid="md1", defaults={"name": "MD1", "coord": Point(100, 100, 0)}
    )
    return player, character


@patch("amc_finance.services.check_treasury_floor", new_callable=AsyncMock, return_value=True)
@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
@patch("amc.game_server.announce", new_callable=AsyncMock)
@patch("amc.special_cargo.announce", new_callable=AsyncMock)
class MoneyLaunderingTests(TestCase):
    async def test_money_delivery_creates_criminal_record(
        self, mock_sc_announce, mock_announce, mock_get_treasury, mock_get_rp_mode, mock_check_floor
    ):
        """Money delivery should create an active CriminalRecord (cleared_at=None)."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 1_000_000

        player, character = await _setup_character("cr1")
        event = _money_cargo_event(character.guid, player.unique_id, payment=10_000)

        await process_event(event, player, character)

        record = await CriminalRecord.objects.filter(character=character).afirst()
        self.assertIsNotNone(record)
        self.assertEqual(record.reason, "Money delivery")
        # Active record has cleared_at = None
        self.assertIsNone(record.cleared_at)
        # Amount should be accumulated
        self.assertEqual(record.amount, 10_000)
        self.assertEqual(record.confiscatable_amount, 8_000)  # 80% of 10_000

    async def test_money_delivery_reuses_existing_criminal_record(
        self, mock_sc_announce, mock_announce, mock_get_treasury, mock_get_rp_mode, mock_check_floor
    ):
        """Subsequent Money deliveries should accumulate on the same active CriminalRecord."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 1_000_000

        player, character = await _setup_character("cr2")

        # Create existing active record
        await CriminalRecord.objects.acreate(
            character=character,
            reason="Money delivery",
            cleared_at=None,  # active
            amount=50_000,
            confiscatable_amount=50_000,
        )

        event = _money_cargo_event(character.guid, player.unique_id, payment=5_000)
        await process_event(event, player, character)

        # Still only one active record
        self.assertEqual(
            await CriminalRecord.objects.filter(character=character, cleared_at__isnull=True).acount(), 1
        )
        record = await CriminalRecord.objects.aget(character=character, cleared_at__isnull=True)
        # Amount should be the original + new delivery
        self.assertEqual(record.amount, 55_000)
        self.assertEqual(record.confiscatable_amount, 54_000)  # 50_000 + 80% of 5_000

    async def test_money_delivery_no_new_wanted_no_laundering_announce(
        self, mock_sc_announce, mock_announce, mock_get_treasury, mock_get_rp_mode, mock_check_floor
    ):
        """A money delivery to a NOT-already-wanted player does not create a Wanted
        record (random wanted trigger is deprecated) and therefore does not populate
        the laundering-announce cache."""
        from amc.models import Wanted
        from django.core.cache import cache

        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 1_000_000

        player, character = await _setup_character("ann")
        event = _money_cargo_event(character.guid, player.unique_id, payment=15_000)
        http_client = AsyncMock()

        await process_event(event, player, character, http_client=http_client)

        # No new Wanted is auto-created for a not-yet-wanted player.
        self.assertEqual(
            await Wanted.objects.filter(character=character, expired_at__isnull=True).acount(),
            0,
            "Fresh illicit delivery must not auto-create a Wanted record",
        )
        # And therefore the laundering-announce cache (set only on new-Wanted creation)
        # is not populated.
        cache_key = f"money_laundered:{character.guid}"
        data = await cache.aget(cache_key)
        self.assertIsNone(data, "Laundering cache must not be set without a new Wanted")

    async def test_money_delivery_refresh_existing_wanted_does_not_announce(
        self, mock_sc_announce, mock_announce, mock_get_treasury, mock_get_rp_mode, mock_check_floor
    ):
        """A money delivery to an ALREADY-wanted player refreshes the record but never
        announces (announce is gated on newly_created, which is False on refresh)."""
        from amc.models import Wanted
        from django.core.cache import cache

        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 1_000_000

        player, character = await _setup_character("deb")
        http_client = AsyncMock()

        # Pre-existing active Wanted → delivery refreshes it (not creates).
        await Wanted.objects.acreate(
            character=character,
            wanted_remaining=60,
        )

        event = _money_cargo_event(character.guid, player.unique_id, payment=20_000)
        await process_event(event, player, character, http_client=http_client)

        # Still exactly one active Wanted (refresh, no duplicate).
        self.assertEqual(
            await Wanted.objects.filter(character=character, expired_at__isnull=True).acount(),
            1,
            "Existing Wanted must be refreshed, not duplicated",
        )
        # Announce cache is only set on newly_created — never on a refresh.
        cache_key = f"money_laundered:{character.guid}"
        data = await cache.aget(cache_key)
        self.assertIsNone(data, "Laundering announce must not fire on a Wanted refresh")

    async def test_money_delivery_treasury_cost(
        self, mock_sc_announce, mock_announce, mock_get_treasury, mock_get_rp_mode, mock_check_floor
    ):
        """Money delivery should deduct 20% of the payment from the treasury."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 1_000_000

        player, character = await _setup_character("tc1")

        initial_balance = Decimal(str(await get_treasury_fund_balance()))

        event = _money_cargo_event(character.guid, player.unique_id, payment=50_000)
        await process_event(event, player, character)

        final_balance = Decimal(str(await get_treasury_fund_balance()))

        expected_cost = Decimal(int(50_000 * 0.20))  # 10,000
        self.assertEqual(initial_balance - final_balance, expected_cost)

    async def test_money_delivery_treasury_cost_multiple_cargos(
        self, mock_sc_announce, mock_announce, mock_get_treasury, mock_get_rp_mode, mock_check_floor
    ):
        """Multiple Money cargos in one event should sum up for treasury cost."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 1_000_000

        player, character = await _setup_character("tc2")

        initial_balance = Decimal(str(await get_treasury_fund_balance()))

        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "PlayerId": str(player.unique_id),
                "Cargos": [
                    {
                        "Net_CargoKey": "Money",
                        "Net_Payment": 10_000,
                        "Net_Weight": 50.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    },
                    {
                        "Net_CargoKey": "Money",
                        "Net_Payment": 10_000,
                        "Net_Weight": 50.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    },
                    {
                        "Net_CargoKey": "Money",
                        "Net_Payment": 10_000,
                        "Net_Weight": 50.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    },
                ],
            },
        }

        await process_event(event, player, character)

        final_balance = Decimal(str(await get_treasury_fund_balance()))
        expected_cost = Decimal(int(30_000 * 0.20))
        self.assertEqual(initial_balance - final_balance, expected_cost)

    async def test_non_money_delivery_no_treasury_cost(
        self, mock_sc_announce, mock_announce, mock_get_treasury, mock_get_rp_mode, mock_check_floor
    ):
        """Non-Money cargo deliveries should not incur treasury cost."""
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 1_000_000

        player, character = await _setup_character("nmc")

        initial_balance = Decimal(str(await get_treasury_fund_balance()))

        event = {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "PlayerId": str(player.unique_id),
                "Cargos": [
                    {
                        "Net_CargoKey": "oranges",
                        "Net_Payment": 50_000,
                        "Net_Weight": 100.0,
                        "Net_Damage": 0.0,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                    }
                ],
            },
        }

        await process_event(event, player, character)

        final_balance = Decimal(str(await get_treasury_fund_balance()))
        self.assertEqual(initial_balance, final_balance)
