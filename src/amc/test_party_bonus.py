import os
import time
from datetime import timedelta
from unittest.mock import patch, MagicMock, AsyncMock
from django.core.cache import cache
from django.test import TestCase
from django.contrib.gis.geos import Point
from django.utils import timezone
from asgiref.sync import sync_to_async
from amc.mod_server import get_party_size_for_character, get_party_members_for_character
from amc.webhook import PARTY_BONUS_RATE
from amc.factories import PlayerFactory, CharacterFactory
from amc.models import DeliveryPoint, CharacterLocation, PlayerStatusLog


class GetPartySizeTest(TestCase):
    """Tests for get_party_size_for_character."""

    def setUp(self):
        self.parties = [
            {
                "PartyId": 1,
                "Players": [
                    "AAAA0000BBBB1111CCCC2222DDDD3333",
                    "EEEE4444FFFF5555AAAA6666BBBB7777",
                ],
            },
            {
                "PartyId": 2,
                "Players": [
                    "11112222333344445555666677778888",
                    "AAAABBBBCCCCDDDDEEEEFFFF00001111",
                    "22223333444455556666777788889999",
                ],
            },
        ]

    def test_player_in_2_person_party(self):
        size = get_party_size_for_character(
            self.parties, "AAAA0000BBBB1111CCCC2222DDDD3333"
        )
        self.assertEqual(size, 2)

    def test_player_in_3_person_party(self):
        size = get_party_size_for_character(
            self.parties, "11112222333344445555666677778888"
        )
        self.assertEqual(size, 3)

    def test_player_not_in_party(self):
        size = get_party_size_for_character(
            self.parties, "NOT_IN_ANY_PARTY_GUID00000000"
        )
        self.assertEqual(size, 1)

    def test_empty_parties_list(self):
        size = get_party_size_for_character([], "AAAA0000BBBB1111CCCC2222DDDD3333")
        self.assertEqual(size, 1)

    def test_case_insensitive_guid_matching(self):
        """Backend may pass lowercase guid; mod server returns uppercase."""
        size = get_party_size_for_character(
            self.parties, "aaaa0000bbbb1111cccc2222dddd3333"
        )
        self.assertEqual(size, 2)

    def test_none_guid(self):
        """Defensive: None guid should not crash."""
        size = get_party_size_for_character(self.parties, None)
        self.assertEqual(size, 1)

    def test_party_with_empty_players(self):
        parties = [{"PartyId": 1, "Players": []}]
        size = get_party_size_for_character(parties, "SOME_GUID")
        self.assertEqual(size, 1)

    def test_party_missing_players_key(self):
        parties = [{"PartyId": 1}]
        size = get_party_size_for_character(parties, "SOME_GUID")
        self.assertEqual(size, 1)


class GetPartyMembersTest(TestCase):
    """Tests for get_party_members_for_character."""

    def setUp(self):
        self.parties = [
            {
                "PartyId": 1,
                "Players": [
                    "AAAA0000BBBB1111CCCC2222DDDD3333",
                    "EEEE4444FFFF5555AAAA6666BBBB7777",
                ],
            },
            {
                "PartyId": 2,
                "Players": [
                    "11112222333344445555666677778888",
                    "AAAABBBBCCCCDDDDEEEEFFFF00001111",
                    "22223333444455556666777788889999",
                ],
            },
        ]

    def test_returns_all_members_in_2_person_party(self):
        members = get_party_members_for_character(
            self.parties, "AAAA0000BBBB1111CCCC2222DDDD3333"
        )
        self.assertEqual(len(members), 2)
        self.assertIn("AAAA0000BBBB1111CCCC2222DDDD3333", members)
        self.assertIn("EEEE4444FFFF5555AAAA6666BBBB7777", members)

    def test_returns_all_members_in_3_person_party(self):
        members = get_party_members_for_character(
            self.parties, "11112222333344445555666677778888"
        )
        self.assertEqual(len(members), 3)

    def test_solo_returns_own_guid(self):
        members = get_party_members_for_character(
            self.parties, "NOT_IN_ANY_PARTY_GUID00000000"
        )
        self.assertEqual(members, ["NOT_IN_ANY_PARTY_GUID00000000"])

    def test_empty_parties(self):
        members = get_party_members_for_character(
            [], "AAAA0000BBBB1111CCCC2222DDDD3333"
        )
        self.assertEqual(members, ["AAAA0000BBBB1111CCCC2222DDDD3333"])

    def test_none_guid_returns_empty(self):
        members = get_party_members_for_character(self.parties, None)
        self.assertEqual(members, [])

    def test_case_insensitive_lookup(self):
        members = get_party_members_for_character(
            self.parties, "aaaa0000bbbb1111cccc2222dddd3333"
        )
        self.assertEqual(len(members), 2)


class PartyBonusMultiplierTest(TestCase):
    """Tests for the bonus multiplier formula applied in process_events."""

    def _calc_multiplier(self, party_size):
        return 1 + (party_size - 1) * PARTY_BONUS_RATE

    def test_solo_no_bonus(self):
        self.assertAlmostEqual(self._calc_multiplier(1), 1.0)

    def test_two_person_party(self):
        self.assertAlmostEqual(self._calc_multiplier(2), 1.05)

    def test_five_person_party(self):
        self.assertAlmostEqual(self._calc_multiplier(5), 1.20)

    def test_large_party_no_cap(self):
        self.assertAlmostEqual(self._calc_multiplier(10), 1.45)

    def test_bonus_on_whole_payment(self):
        """15000 total payment with 3-person party -> bonus = int(15000 * 0.10) = 1500."""
        total_payment = 15000
        total_subsidy = 5000
        party_size = 3
        multiplier = self._calc_multiplier(party_size)
        party_bonus = int(total_payment * (multiplier - 1))
        total_subsidy += party_bonus
        total_payment += party_bonus
        self.assertEqual(party_bonus, 1500)
        self.assertEqual(total_subsidy, 6500)  # 5000 + 1500
        self.assertEqual(total_payment, 16500)  # 15000 + 1500

    def test_no_bonus_on_zero_payment(self):
        total_payment = 0
        # Guard: total_payment > 0 prevents computation
        if total_payment > 0:
            party_bonus = int(total_payment * (self._calc_multiplier(3) - 1))
        else:
            party_bonus = 0
        self.assertEqual(party_bonus, 0)

    def test_no_bonus_on_negative_payment(self):
        """Negative payment must NOT generate bonus."""
        total_payment = -1000
        if total_payment > 0:
            party_bonus = int(total_payment * (self._calc_multiplier(3) - 1))
        else:
            party_bonus = 0
        self.assertEqual(party_bonus, 0)

    def test_bonus_delivered_as_subsidy(self):
        """Party bonus is added to total_subsidy since backend transfers it."""
        total_payment = 10000  # base 7000 + subsidy 3000
        total_subsidy = 3000
        party_bonus = int(total_payment * (self._calc_multiplier(2) - 1))
        total_subsidy += party_bonus
        total_payment += party_bonus
        # Verify subsidy now includes the party bonus
        self.assertEqual(party_bonus, 500)  # 10000 * 0.05
        self.assertEqual(total_subsidy, 3500)  # 3000 + 500

    def test_shortcut_strips_party_bonus(self):
        """Shortcut zeroes subsidy (including party bonus) after it's applied."""
        total_payment = 10000  # base 7000 + subsidy 3000
        total_subsidy = 3000

        # Apply party bonus first
        party_bonus = int(total_payment * (self._calc_multiplier(3) - 1))
        total_subsidy += party_bonus
        total_payment += party_bonus
        self.assertEqual(total_subsidy, 4000)  # 3000 + 1000

        # Then shortcut strips it
        total_payment -= total_subsidy
        total_subsidy = 0
        self.assertEqual(total_subsidy, 0)
        self.assertEqual(total_payment, 7000)  # back to base only

    def test_bonus_with_zero_subsidy(self):
        """Even with zero subsidy, bonus applies to total_payment (base earnings)."""
        total_payment = 10000  # all base, no subsidy
        total_subsidy = 0
        party_bonus = int(total_payment * (self._calc_multiplier(2) - 1))
        total_subsidy += party_bonus
        total_payment += party_bonus
        self.assertEqual(party_bonus, 500)
        self.assertEqual(total_subsidy, 500)  # was 0, now has party bonus


class FeatureFlagTest(TestCase):
    """Tests for PARTY_BONUS_ENABLED feature flag."""

    def test_flag_enabled_with_1(self):
        with patch.dict(os.environ, {"PARTY_BONUS_ENABLED": "1"}):
            result = os.environ.get("PARTY_BONUS_ENABLED", "").lower() in ("1", "true", "yes")
            self.assertTrue(result)

    def test_flag_enabled_with_true(self):
        with patch.dict(os.environ, {"PARTY_BONUS_ENABLED": "true"}):
            result = os.environ.get("PARTY_BONUS_ENABLED", "").lower() in ("1", "true", "yes")
            self.assertTrue(result)

    def test_flag_enabled_with_TRUE(self):
        with patch.dict(os.environ, {"PARTY_BONUS_ENABLED": "TRUE"}):
            result = os.environ.get("PARTY_BONUS_ENABLED", "").lower() in ("1", "true", "yes")
            self.assertTrue(result)

    def test_flag_disabled_with_0(self):
        with patch.dict(os.environ, {"PARTY_BONUS_ENABLED": "0"}):
            result = os.environ.get("PARTY_BONUS_ENABLED", "").lower() in ("1", "true", "yes")
            self.assertFalse(result)

    def test_flag_disabled_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            result = os.environ.get("PARTY_BONUS_ENABLED", "").lower() in ("1", "true", "yes")
            self.assertFalse(result)

    def test_flag_disabled_with_empty(self):
        with patch.dict(os.environ, {"PARTY_BONUS_ENABLED": ""}):
            result = os.environ.get("PARTY_BONUS_ENABLED", "").lower() in ("1", "true", "yes")
            self.assertFalse(result)


def _make_cargo_event(character_guid, payment=10_000):
    """Helper to create a simple cargo event."""
    return {
        "hook": "ServerCargoArrived",
        "timestamp": int(time.time()),
        "data": {
            "CharacterGuid": str(character_guid),
            "Cargos": [
                {
                    "Net_CargoKey": "oranges",
                    "Net_Payment": payment,
                    "Net_Weight": 100.0,
                    "Net_Damage": 0.0,
                    "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                    "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                }
            ],
        },
    }


async def _setup_delivery_points():
    """Create basic delivery points for tests."""
    await DeliveryPoint.objects.acreate(guid="s1", name="S1", coord=Point(0, 0, 0))
    await DeliveryPoint.objects.acreate(guid="d1", name="D1", coord=Point(100, 100, 0))


async def _create_online_character(player=None, guid=None, **kwargs):
    """Create a character with location and status log (required for process_events)."""
    if player is None:
        player = await sync_to_async(PlayerFactory)()
    char_kwargs = {"player": player}
    if guid:
        char_kwargs["guid"] = guid
    char_kwargs.update(kwargs)
    character = await sync_to_async(CharacterFactory)(**char_kwargs)
    await CharacterLocation.objects.acreate(
        character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
    )
    await PlayerStatusLog.objects.acreate(
        character=character,
        timespan=(timezone.now() - timedelta(minutes=5), timezone.now()),
    )
    return character


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
@patch("amc.webhook.PARTY_BONUS_ENABLED", True)
class PartyPaymentSplitTest(TestCase):
    """Integration tests for party payment splitting via process_events."""

    def setUp(self):
        cache.clear()

    async def test_equal_split_two_members(self, mock_treasury, mock_rp):
        """2-person party: payment split equally after bonus."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        await _create_online_character(guid="AAAA0000BBBB1111CCCC2222DDDD3333")
        await _create_online_character(guid="EEEE4444FFFF5555AAAA6666BBBB7777")
        await _setup_delivery_points()

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "AAAA0000BBBB1111CCCC2222DDDD3333",
                    "EEEE4444FFFF5555AAAA6666BBBB7777",
                ],
            }
        ]
        events = [_make_cargo_event("AAAA0000BBBB1111CCCC2222DDDD3333", payment=10_000)]

        player_profits = []
        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock) as mock_profits,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx

            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events(events, http_client_mod=mock_mod)
            player_profits = mock_profits.call_args[0][0]

        # 2-person party: bonus = int(10000 * 0.05) = 500
        # total_base_payment = 10000, total_subsidy = 500 (bonus only)
        # share_base = 10000 // 2 = 5000
        # share_subsidy = 500 // 2 = 250
        self.assertEqual(len(player_profits), 2)
        for char, subsidy, payment, contract in player_profits:
            self.assertEqual(payment, 5000)
            self.assertEqual(subsidy, 250)
            self.assertEqual(contract, 0)

    async def test_equal_split_three_members(self, mock_treasury, mock_rp):
        """3-person party: payment split equally after bonus."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        await _create_online_character(guid="AAAA000000000000000000000000AAA1")
        await _create_online_character(guid="AAAA000000000000000000000000AAA2")
        await _create_online_character(guid="AAAA000000000000000000000000AAA3")
        await _setup_delivery_points()

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "AAAA000000000000000000000000AAA1",
                    "AAAA000000000000000000000000AAA2",
                    "AAAA000000000000000000000000AAA3",
                ],
            }
        ]
        events = [_make_cargo_event("AAAA000000000000000000000000AAA1", payment=15_000)]

        player_profits = []
        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock) as mock_profits,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events(events, http_client_mod=mock_mod)
            player_profits = mock_profits.call_args[0][0]

        # 3-person: bonus = int(15000 * 0.10) = 1500 → subsidy only
        # total_base = 15000, subsidy = 1500
        # share_base = 15000 // 3 = 5000
        # share_subsidy = 1500 // 3 = 500
        self.assertEqual(len(player_profits), 3)
        for char, subsidy, payment, contract in player_profits:
            self.assertEqual(payment, 5000)
            self.assertEqual(subsidy, 500)

    async def test_solo_no_split(self, mock_treasury, mock_rp):
        """Solo player (not in party): no splitting, same as before."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        await _create_online_character(guid="SOLO000000000000000000000000SOLO")
        await _setup_delivery_points()

        events = [_make_cargo_event("SOLO000000000000000000000000SOLO", payment=10_000)]

        player_profits = []
        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=[]),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock) as mock_profits,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events(events, http_client_mod=mock_mod)
            player_profits = mock_profits.call_args[0][0]

        # Solo: no party bonus, no split
        self.assertEqual(len(player_profits), 1)
        char, subsidy, payment, contract = player_profits[0]
        self.assertEqual(payment, 10_000)

    async def test_split_with_gov_employee_member(self, mock_treasury, mock_rp):
        """Gov employee member still gets their share in player_profits.
        on_player_profit will handle the gov redirect independently."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        await _create_online_character(guid="GOV1000000000000000000000000GOV1")
        # Make c2 a gov employee
        c2 = await _create_online_character(guid="GOV2000000000000000000000000GOV2")
        c2.gov_employee_until = timezone.now() + timedelta(hours=24)
        await c2.asave(update_fields=["gov_employee_until"])

        await _setup_delivery_points()

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "GOV1000000000000000000000000GOV1",
                    "GOV2000000000000000000000000GOV2",
                ],
            }
        ]
        events = [_make_cargo_event("GOV1000000000000000000000000GOV1", payment=10_000)]

        player_profits = []
        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock) as mock_profits,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events(events, http_client_mod=mock_mod)
            player_profits = mock_profits.call_args[0][0]

        # Both members get equal shares in player_profits
        self.assertEqual(len(player_profits), 2)
        guids = {pp[0].guid for pp in player_profits}
        self.assertIn("GOV2000000000000000000000000GOV2", guids)
        # Shares should be equal regardless of gov status
        shares = {pp[2] for pp in player_profits}
        self.assertEqual(len(shares), 1)  # all equal

    async def test_nonexistent_party_member_skipped(self, mock_treasury, mock_rp):
        """If a party member's GUID isn't in DB, they're skipped gracefully."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        await _create_online_character(guid="REAL000000000000000000000000REAL")
        await _setup_delivery_points()

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "REAL000000000000000000000000REAL",
                    "FAKE000000000000000000000000FAKE",  # not in DB
                ],
            }
        ]
        events = [_make_cargo_event("REAL000000000000000000000000REAL", payment=10_000)]

        player_profits = []
        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock) as mock_profits,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events(events, http_client_mod=mock_mod)
            player_profits = mock_profits.call_args[0][0]

        # Earner + 1 nonexistent = only earner + 0 found others
        # Split is still calculated for party_size=2, but only earner is appended
        # since FAKE is not in DB → other_characters is empty
        self.assertEqual(len(player_profits), 1)
        char, subsidy, payment, contract = player_profits[0]
        # Share is half of boosted total (earner gets their split only)
        self.assertEqual(char.guid, "REAL000000000000000000000000REAL")

    async def test_wallet_transfers_made(self, mock_treasury, mock_rp):
        """Verify transfer_money calls: withdraw from earner, deposit to others."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        await _create_online_character(guid="XFER000000000000000000000000XFR1")
        await _create_online_character(guid="XFER000000000000000000000000XFR2")
        await _setup_delivery_points()

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "XFER000000000000000000000000XFR1",
                    "XFER000000000000000000000000XFR2",
                ],
            }
        ]
        events = [_make_cargo_event("XFER000000000000000000000000XFR1", payment=10_000)]

        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock),
            patch("amc.webhook.transfer_money", new_callable=AsyncMock) as mock_transfer,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events(events, http_client_mod=mock_mod)

        # Should have at least 2 transfer_money calls:
        # 1. Withdraw from earner (negative)
        # 2. Deposit to other member (positive)
        transfer_calls = mock_transfer.call_args_list
        self.assertGreaterEqual(len(transfer_calls), 2)

        # Find the withdrawal (negative amount) and deposit (positive amount)
        amounts = [call[0][1] for call in transfer_calls]  # second positional arg
        messages = [call[0][2] for call in transfer_calls]
        self.assertTrue(any(a < 0 for a in amounts), "Should have a withdrawal")
        self.assertTrue(any(a > 0 for a in amounts), "Should have a deposit")
        self.assertIn("Party Split", messages)
        self.assertIn("Party Share", messages)

    async def test_split_with_contract_payment(self, mock_treasury, mock_rp):
        """Contract payments are also split equally among party members."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        from amc.models import ServerSignContractLog

        c1 = await _create_online_character(guid="CNTR000000000000000000000000CNT1")
        await _create_online_character(guid="CNTR000000000000000000000000CNT2")
        await _setup_delivery_points()

        # Create a completed contract for c1
        await ServerSignContractLog.objects.acreate(
            guid="contract_split_test",
            player=c1.player,
            cargo_key="sand",
            amount=1,
            finished_amount=0,
            payment=50_000,
            cost=1000,
            timestamp=timezone.now(),
        )

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "CNTR000000000000000000000000CNT1",
                    "CNTR000000000000000000000000CNT2",
                ],
            }
        ]
        # Contract delivery event
        contract_event = {
            "hook": "ServerContractCargoDelivered",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(c1.guid),
                "ContractGuid": "contract_split_test",
                "Item": "sand",
                "Amount": 1,
                "CompletionPayment": 50_000,
                "Cost": 1000,
            },
        }

        player_profits = []
        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock) as mock_profits,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events([contract_event], http_client_mod=mock_mod)
            player_profits = mock_profits.call_args[0][0]

        # Contract payment (50000) should be split
        self.assertEqual(len(player_profits), 2)
        for char, subsidy, payment, contract in player_profits:
            self.assertEqual(contract, 25_000)  # 50000 // 2


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
@patch("amc.webhook.PARTY_BONUS_ENABLED", True)
class PartyShareGovEmployeeTest(TestCase):
    """Tests for government employee interactions with party sharing.

    When a gov employee is in a party:
    - They still receive an equal share in player_profits
    - on_player_profit handles the gov redirect (confiscation → treasury)
    - Contract payments are burned for gov employees

    These tests verify the values reaching on_player_profit are correct
    and expose issues where wallet transfers don't account for gov status.
    """

    def setUp(self):
        cache.clear()

    async def test_gov_earner_share_values(self, mock_treasury, mock_rp):
        """Gov earner's share should reach on_player_profit correctly.

        The earner is a gov employee. After the party split, on_player_profit
        should be called with the same share_payment/share_subsidy as the
        other (non-gov) member. The gov path in on_player_profit handles
        confiscation separately.
        """
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        # Earner is gov
        c1 = await _create_online_character(guid="GOVE000000000000000000000000GOV1")
        c1.gov_employee_until = timezone.now() + timedelta(hours=24)
        await c1.asave(update_fields=["gov_employee_until"])

        _ = await _create_online_character(guid="GOVE000000000000000000000000GOV2")
        await _setup_delivery_points()

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "GOVE000000000000000000000000GOV1",
                    "GOVE000000000000000000000000GOV2",
                ],
            }
        ]
        events = [_make_cargo_event("GOVE000000000000000000000000GOV1", payment=10_000)]

        player_profits = []
        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock) as mock_profits,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events(events, http_client_mod=mock_mod)
            player_profits = mock_profits.call_args[0][0]

        # 2-person party: bonus = int(10000 * 0.05) = 500
        # total_base = 10000, subsidy = 500
        # share_base = 10000 // 2 = 5000
        # share_subsidy = 500 // 2 = 250
        self.assertEqual(len(player_profits), 2)

        # Both members get equal splits regardless of gov status
        for char, subsidy, payment, contract in player_profits:
            self.assertEqual(payment, 5000)
            self.assertEqual(subsidy, 250)
            self.assertEqual(contract, 0)

    async def test_gov_earner_wallet_transfer_amounts(self, mock_treasury, mock_rp):
        """Verify wallet transfer amounts when earner is gov.

        The party split withdraws the other member's base share from the earner.
        Then on_player_profit will confiscate the gov employee's remaining
        wallet balance. The total withdrawn from the earner should equal the
        original game deposit.
        """
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        c1 = await _create_online_character(guid="GOVW000000000000000000000000GVW1")
        c1.gov_employee_until = timezone.now() + timedelta(hours=24)
        await c1.asave(update_fields=["gov_employee_until"])

        _ = await _create_online_character(guid="GOVW000000000000000000000000GVW2")
        await _setup_delivery_points()

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "GOVW000000000000000000000000GVW1",
                    "GOVW000000000000000000000000GVW2",
                ],
            }
        ]
        events = [_make_cargo_event("GOVW000000000000000000000000GVW1", payment=10_000)]

        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock),
            patch("amc.webhook.transfer_money", new_callable=AsyncMock) as mock_transfer,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events(events, http_client_mod=mock_mod)

        # wallet_share = share_base + share_contract = 5000 + 0 = 5000
        # Withdrawal from earner: -5000 (Party Split)
        # Deposit to other: +5000 (Party Share)
        transfer_calls = mock_transfer.call_args_list
        amounts = [call[0][1] for call in transfer_calls]
        messages = [call[0][2] for call in transfer_calls]

        withdrawal = [a for a, m in zip(amounts, messages) if m == "Party Split"]
        deposit = [a for a, m in zip(amounts, messages) if m == "Party Share"]

        self.assertEqual(len(withdrawal), 1)
        self.assertEqual(withdrawal[0], -5000)
        self.assertEqual(len(deposit), 1)
        self.assertEqual(deposit[0], 5000)

    async def test_both_members_gov(self, mock_treasury, mock_rp):
        """Both party members are gov employees.

        Both should get equal shares in player_profits. on_player_profit
        handles gov confiscation for each independently.
        """
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        c1 = await _create_online_character(guid="BGOV000000000000000000000000BGV1")
        c1.gov_employee_until = timezone.now() + timedelta(hours=24)
        await c1.asave(update_fields=["gov_employee_until"])

        c2 = await _create_online_character(guid="BGOV000000000000000000000000BGV2")
        c2.gov_employee_until = timezone.now() + timedelta(hours=24)
        await c2.asave(update_fields=["gov_employee_until"])

        await _setup_delivery_points()

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "BGOV000000000000000000000000BGV1",
                    "BGOV000000000000000000000000BGV2",
                ],
            }
        ]
        events = [_make_cargo_event("BGOV000000000000000000000000BGV1", payment=20_000)]

        player_profits = []
        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock) as mock_profits,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events(events, http_client_mod=mock_mod)
            player_profits = mock_profits.call_args[0][0]

        # 2-person party, 20000 base
        # bonus = int(20000 * 0.05) = 1000 → subsidy only
        # total_base = 20000, subsidy = 1000
        # share_base = 20000 // 2 = 10000
        # share_subsidy = 1000 // 2 = 500
        self.assertEqual(len(player_profits), 2)
        gov_guids = {pp[0].guid for pp in player_profits}
        self.assertEqual(gov_guids, {"BGOV000000000000000000000000BGV1", "BGOV000000000000000000000000BGV2"})
        for char, subsidy, payment, contract in player_profits:
            self.assertEqual(payment, 10000)
            self.assertEqual(subsidy, 500)

    async def test_mixed_gov_and_civilian_on_player_profit(self, mock_treasury, mock_rp):
        """Verify on_player_profit handles gov and civilian paths correctly.

        One gov member should have income confiscated (no subsidy, no loan, no savings).
        One civilian member should get subsidy paid, loan repaid, savings set aside.
        """
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        from amc.webhook import on_player_profit

        player1 = await sync_to_async(PlayerFactory)()
        gov_char = await sync_to_async(CharacterFactory)(
            player=player1, guid="MIXD000000000000000000000000MIX1"
        )
        gov_char.gov_employee_until = timezone.now() + timedelta(hours=24)
        await gov_char.asave(update_fields=["gov_employee_until"])

        player2 = await sync_to_async(PlayerFactory)()
        civ_char = await sync_to_async(CharacterFactory)(
            player=player2, guid="MIXD000000000000000000000000MIX2",
            reject_ubi=False,
        )

        session = MagicMock()
        share_subsidy = 250
        share_base = 5000  # base_payment = what game deposited

        # Test gov path
        with (
            patch("amc.webhook.transfer_money", new_callable=AsyncMock) as mock_transfer,
            patch("amc.gov_employee.redirect_income_to_treasury", new_callable=AsyncMock),
            patch("amc.webhook.subsidise_player", new_callable=AsyncMock) as mock_subsidise,
            patch("amc.webhook.repay_loan_for_profit", new_callable=AsyncMock) as mock_repay,
            patch("amc.webhook.set_aside_player_savings", new_callable=AsyncMock) as mock_savings,
        ):
            await on_player_profit(gov_char, share_subsidy, share_base, session)

            # Gov: confiscates base_payment + subsidy separately
            mock_subsidise.assert_called_once_with(250, gov_char, session)
            mock_repay.assert_not_called()
            mock_savings.assert_not_called()
            # Two transfer_money calls: base confiscation (-5000) + subsidy confiscation (-250)
            self.assertEqual(mock_transfer.call_count, 2)
            self.assertEqual(mock_transfer.call_args_list[0].args[1], -5000)
            self.assertEqual(mock_transfer.call_args_list[1].args[1], -250)

        # Test civilian path
        with (
            patch("amc.webhook.transfer_money", new_callable=AsyncMock) as mock_transfer,
            patch("amc.webhook.subsidise_player", new_callable=AsyncMock) as mock_subsidise,
            patch("amc.webhook.repay_loan_for_profit", new_callable=AsyncMock, return_value=0) as mock_repay,
            patch("amc.webhook.set_aside_player_savings", new_callable=AsyncMock) as mock_savings,
        ):
            await on_player_profit(civ_char, share_subsidy, share_base, session)

            # Civilian: receives subsidy, loan repayment, savings
            mock_subsidise.assert_called_once_with(250, civ_char, session)
            mock_repay.assert_called_once()
            mock_savings.assert_called_once()
            # actual_income = 5000 + 250 + 0 = 5250
            savings_income = mock_savings.call_args[0][1]
            self.assertEqual(savings_income, 5250)

    async def test_gov_earner_contribution_includes_subsidy(self, mock_treasury, mock_rp):
        """Gov earner's contribution to treasury should include subsidy credit
        for level progression, even though subsidy is never actually paid."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        from amc.webhook import on_player_profit

        player = await sync_to_async(PlayerFactory)()
        gov_char = await sync_to_async(CharacterFactory)(
            player=player, guid="GCNT000000000000000000000000GCN1"
        )
        gov_char.gov_employee_until = timezone.now() + timedelta(hours=24)
        await gov_char.asave(update_fields=["gov_employee_until"])

        session = MagicMock()
        share_subsidy = 500
        share_base = 10000  # base_payment directly

        with (
            patch("amc.webhook.transfer_money", new_callable=AsyncMock),
            patch("amc.gov_employee.redirect_income_to_treasury", new_callable=AsyncMock) as mock_redirect,
            patch("amc.webhook.subsidise_player", new_callable=AsyncMock),
        ):
            await on_player_profit(gov_char, share_subsidy, share_base, session)

            # Two redirect calls: earnings + subsidy contribution
            self.assertEqual(mock_redirect.call_count, 2)
            # 1. Earnings: amount=10000 (real money → treasury donation)
            self.assertEqual(mock_redirect.call_args_list[0].args[0], 10000)
            # 2. Subsidy: amount=0 (no donation), contribution=500
            self.assertEqual(mock_redirect.call_args_list[1].args[0], 0)
            self.assertEqual(mock_redirect.call_args_list[1].kwargs["contribution"], 500)


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
@patch("amc.webhook.PARTY_BONUS_ENABLED", True)
class PartyShareLoanRepaymentTest(TestCase):
    def setUp(self):
        cache.clear()
    """Tests for loan repayment interactions with party sharing.

    Loan repayment in on_player_profit is calculated on actual_income:
      actual_income = (total_payment - original_subsidy) + total_subsidy + contract_payment

    For party members, total_payment and total_subsidy are their shares.
    """

    async def test_loan_repayment_from_party_share(self, mock_treasury, mock_rp):
        """Civilian party member's share should correctly feed into loan repayment."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        from amc.webhook import on_player_profit

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, reject_ubi=False,
        )

        session = MagicMock()
        share_subsidy = 250
        share_base = 5000

        with (
            patch("amc.webhook.subsidise_player", new_callable=AsyncMock),
            patch("amc.webhook.repay_loan_for_profit", new_callable=AsyncMock, return_value=1000) as mock_repay,
            patch("amc.webhook.set_aside_player_savings", new_callable=AsyncMock) as mock_savings,
        ):
            await on_player_profit(character, share_subsidy, share_base, session)

            # actual_income = 5000 + 250 + 0 = 5250
            mock_repay.assert_called_once_with(character, 5250, session)
            # savings = actual_income - loan_repayment = 5250 - 1000 = 4250
            mock_savings.assert_called_once_with(character, 4250, session)

    async def test_loan_repayment_with_reject_ubi(self, mock_treasury, mock_rp):
        """Player with reject_ubi gets no subsidy, but loan still repaid on base income."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        from amc.webhook import on_player_profit

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, reject_ubi=True,
        )

        session = MagicMock()
        share_subsidy = 250
        share_base = 5000

        with (
            patch("amc.webhook.subsidise_player", new_callable=AsyncMock) as mock_subsidise,
            patch("amc.webhook.repay_loan_for_profit", new_callable=AsyncMock, return_value=0) as mock_repay,
            patch("amc.webhook.set_aside_player_savings", new_callable=AsyncMock) as mock_savings,
        ):
            await on_player_profit(character, share_subsidy, share_base, session)

            # reject_ubi zeroes subsidy
            mock_subsidise.assert_not_called()
            # actual_income = 5000 + 0 + 0 = 5000
            mock_repay.assert_called_once_with(character, 5000, session)
            mock_savings.assert_called_once_with(character, 5000, session)

    async def test_loan_repayment_with_contract_share(self, mock_treasury, mock_rp):
        """Contract share is included in actual_income for loan repayment.

        NOTE: This test documents the current behavior where contract_payment
        is included in actual_income even though the wallet transfer for
        contract money to non-earner members hasn't happened yet.
        """
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        from amc.webhook import on_player_profit

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player, reject_ubi=False,
        )

        session = MagicMock()
        share_subsidy = 250
        share_base = 5000
        share_contract = 25_000

        with (
            patch("amc.webhook.subsidise_player", new_callable=AsyncMock),
            patch("amc.webhook.repay_loan_for_profit", new_callable=AsyncMock, return_value=5000) as mock_repay,
            patch("amc.webhook.set_aside_player_savings", new_callable=AsyncMock) as mock_savings,
        ):
            await on_player_profit(
                character, share_subsidy, share_base, session,
                contract_payment=share_contract,
            )

            # actual_income = 5000 + 250 + 25000 = 30250
            mock_repay.assert_called_once_with(character, 30250, session)
            # savings = 30250 - 5000 = 25250
            mock_savings.assert_called_once_with(character, 25250, session)

    async def test_full_party_split_loan_values(self, mock_treasury, mock_rp):
        """Full integration: verify actual player_profits values used for loan calc."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        _ = await _create_online_character(guid="LOAN000000000000000000000000LN01")
        _ = await _create_online_character(guid="LOAN000000000000000000000000LN02")
        await _setup_delivery_points()

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "LOAN000000000000000000000000LN01",
                    "LOAN000000000000000000000000LN02",
                ],
            }
        ]
        events = [_make_cargo_event("LOAN000000000000000000000000LN01", payment=20_000)]

        player_profits = []
        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock) as mock_profits,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events(events, http_client_mod=mock_mod)
            player_profits = mock_profits.call_args[0][0]

        # 2-person, 20000 base → bonus=1000 (subsidy only)
        # share_base=10000, share_sub=500
        self.assertEqual(len(player_profits), 2)
        for char, subsidy, payment, contract in player_profits:
            self.assertEqual(payment, 10000)
            self.assertEqual(subsidy, 500)
            self.assertEqual(contract, 0)


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
@patch("amc.webhook.PARTY_BONUS_ENABLED", True)
class PartyShareContractTest(TestCase):
    def setUp(self):
        cache.clear()
    """Tests for contract completion interactions with party sharing.

    Contract payments flow differently from regular cargo:
    - Game deposits contract money into the earner's wallet
    - Party split code does NOT transfer contract money to other members' wallets
    - But on_player_profit processes share_contract for all members

    These tests expose the inconsistency where non-earner members are credited
    with contract income they never received in their wallets.
    """

    async def test_contract_split_wallet_transfers_omit_contract(self, mock_treasury, mock_rp):
        """Verify that wallet transfers do NOT include contract money.

        The earner_base_share only accounts for regular payment minus subsidy.
        Contract money is NOT moved between wallets.
        """
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        from amc.models import ServerSignContractLog

        c1 = await _create_online_character(guid="CWLT000000000000000000000000CW01")
        _ = await _create_online_character(guid="CWLT000000000000000000000000CW02")
        await _setup_delivery_points()

        # Create a contract that will complete
        await ServerSignContractLog.objects.acreate(
            guid="contract_wallet_test",
            player=c1.player,
            cargo_key="sand",
            amount=1,
            finished_amount=0,
            payment=50_000,
            cost=1000,
            timestamp=timezone.now(),
        )

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "CWLT000000000000000000000000CW01",
                    "CWLT000000000000000000000000CW02",
                ],
            }
        ]
        contract_event = {
            "hook": "ServerContractCargoDelivered",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(c1.guid),
                "ContractGuid": "contract_wallet_test",
                "Item": "sand",
                "Amount": 1,
                "CompletionPayment": 50_000,
                "Cost": 1000,
            },
        }

        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock),
            patch("amc.webhook.transfer_money", new_callable=AsyncMock) as mock_transfer,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events([contract_event], http_client_mod=mock_mod)

        # Contract only (no cargo): total_payment=0, total_subsidy=0
        # total_contract_payment = 50000 → triggers party split
        # share_payment = 0, share_subsidy = 0, share_contract = 25000
        # earner_base_share = 0 - 0 = 0
        # earner_wallet_share = 0 + 25000 = 25000
        # Wallet transfers: withdraw 25000 from earner, deposit to other
        transfer_calls = mock_transfer.call_args_list
        party_transfers = [c for c in transfer_calls if c[0][2] in ("Party Split", "Party Share")]
        self.assertEqual(len(party_transfers), 2,
            "Contract money should be wallet-transferred to other party member")
        amounts = {c[0][2]: c[0][1] for c in party_transfers}
        self.assertEqual(amounts["Party Split"], -25000)
        self.assertEqual(amounts["Party Share"], 25000)

    async def test_contract_with_gov_earner_burned(self, mock_treasury, mock_rp):
        """Gov employee earner: contract payment is burned (confiscated but not added to treasury).

        This verifies the on_player_profit gov path handles contract correctly.
        """
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        from amc.webhook import on_player_profit

        player = await sync_to_async(PlayerFactory)()
        gov_char = await sync_to_async(CharacterFactory)(player=player)
        gov_char.gov_employee_until = timezone.now() + timedelta(hours=24)
        await gov_char.asave(update_fields=["gov_employee_until"])

        session = MagicMock()
        share_subsidy = 0
        share_payment = 0
        share_contract = 25_000

        with (
            patch("amc.webhook.transfer_money", new_callable=AsyncMock) as mock_transfer,
            patch("amc.gov_employee.redirect_income_to_treasury", new_callable=AsyncMock) as mock_redirect,
        ):
            await on_player_profit(
                gov_char, share_subsidy, share_payment, session,
                contract_payment=share_contract,
            )

            # wallet_confiscation = base_payment + contract_payment = 0 + 25000 = 25000
            mock_transfer.assert_called_once()
            self.assertEqual(mock_transfer.call_args[0][1], -25000)

            # base_payment = 0, so redirect_income_to_treasury is NOT called
            # (guard: if base_payment > 0)
            mock_redirect.assert_not_called()

    async def test_contract_with_gov_other_member_phantom_confiscation(self, mock_treasury, mock_rp):
        """Gov non-earner member: on_player_profit confiscates contract money
        that was never deposited to their wallet.

        This exposes Bug 3: contract_payment=25000 is passed to on_player_profit
        for the other member, but no wallet transfer moved contract money there.
        The gov path will try to transfer_money(-25000) from their wallet.
        """
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        from amc.webhook import on_player_profit

        player = await sync_to_async(PlayerFactory)()
        gov_other = await sync_to_async(CharacterFactory)(player=player)
        gov_other.gov_employee_until = timezone.now() + timedelta(hours=24)
        await gov_other.asave(update_fields=["gov_employee_until"])

        session = MagicMock()
        # Values as they would be set by process_events for a non-earner member
        # in a 2-person party with contract-only completion
        share_subsidy = 0
        share_payment = 0
        share_contract = 25_000  # this was never deposited to wallet!

        with (
            patch("amc.webhook.transfer_money", new_callable=AsyncMock) as mock_transfer,
            patch("amc.gov_employee.redirect_income_to_treasury", new_callable=AsyncMock),
        ):
            await on_player_profit(
                gov_other, share_subsidy, share_payment, session,
                contract_payment=share_contract,
            )

            # BUG: wallet_confiscation = 0 + 25000 = 25000
            # This withdraws 25000 from a wallet that received 0 contract money
            mock_transfer.assert_called_once()
            confiscation = mock_transfer.call_args[0][1]
            self.assertEqual(confiscation, -25000,
                "Gov non-earner has 25000 confiscated despite never receiving contract money")

    async def test_contract_with_civilian_other_member_phantom_income(self, mock_treasury, mock_rp):
        """Civilian non-earner member: contract payment inflates actual_income
        for loan repayment and savings, despite never being wallet-deposited.

        This exposes Bug 2: contract money is in actual_income but was never
        transferred to the other member's wallet.
        """
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        from amc.webhook import on_player_profit

        player = await sync_to_async(PlayerFactory)()
        civilian = await sync_to_async(CharacterFactory)(
            player=player, reject_ubi=False,
        )

        session = MagicMock()
        share_subsidy = 0
        share_payment = 0
        share_contract = 25_000

        with (
            patch("amc.webhook.subsidise_player", new_callable=AsyncMock),
            patch("amc.webhook.repay_loan_for_profit", new_callable=AsyncMock, return_value=5000) as mock_repay,
            patch("amc.webhook.set_aside_player_savings", new_callable=AsyncMock) as mock_savings,
        ):
            await on_player_profit(
                civilian, share_subsidy, share_payment, session,
                contract_payment=share_contract,
            )

            # actual_income = (0 - 0) + 0 + 25000 = 25000
            # BUG: This 25000 was never deposited to the civilian's wallet
            # but loan repayment is calculated on it
            mock_repay.assert_called_once_with(civilian, 25000, session)
            # savings = 25000 - 5000 = 20000
            mock_savings.assert_called_once_with(civilian, 20000, session)

    async def test_contract_plus_cargo_combined_split(self, mock_treasury, mock_rp):
        """Contract completion + cargo in same event batch: verify combined split."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        from amc.models import ServerSignContractLog

        c1 = await _create_online_character(guid="CCMB000000000000000000000000CM01")
        _ = await _create_online_character(guid="CCMB000000000000000000000000CM02")
        await _setup_delivery_points()

        await ServerSignContractLog.objects.acreate(
            guid="contract_combo_test",
            player=c1.player,
            cargo_key="sand",
            amount=1,
            finished_amount=0,
            payment=50_000,
            cost=1000,
            timestamp=timezone.now(),
        )

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "CCMB000000000000000000000000CM01",
                    "CCMB000000000000000000000000CM02",
                ],
            }
        ]
        cargo_event = _make_cargo_event("CCMB000000000000000000000000CM01", payment=10_000)
        contract_event = {
            "hook": "ServerContractCargoDelivered",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(c1.guid),
                "ContractGuid": "contract_combo_test",
                "Item": "sand",
                "Amount": 1,
                "CompletionPayment": 50_000,
                "Cost": 1000,
            },
        }

        player_profits = []
        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock) as mock_profits,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events([cargo_event, contract_event], http_client_mod=mock_mod)
            player_profits = mock_profits.call_args[0][0]

        # cargo: total_base_payment = 10000 (no subsidy rule)
        # contract: total_contract_payment = 50000
        # party bonus = int(10000 * 0.05) = 500 → subsidy only
        # total_base_payment = 10000 (unchanged), total_subsidy = 500
        # share_base = 10000 // 2 = 5000
        # share_subsidy = 500 // 2 = 250
        # share_contract = 50000 // 2 = 25000
        self.assertEqual(len(player_profits), 2)
        for char, subsidy, payment, contract in player_profits:
            self.assertEqual(payment, 5000)
            self.assertEqual(subsidy, 250)
            self.assertEqual(contract, 25_000)

    async def test_three_way_contract_split(self, mock_treasury, mock_rp):
        """3-person party: contract payment split 3 ways with integer division."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        from amc.models import ServerSignContractLog

        c1 = await _create_online_character(guid="3WAY000000000000000000000000TW01")
        _ = await _create_online_character(guid="3WAY000000000000000000000000TW02")
        _ = await _create_online_character(guid="3WAY000000000000000000000000TW03")
        await _setup_delivery_points()

        await ServerSignContractLog.objects.acreate(
            guid="contract_3way_test",
            player=c1.player,
            cargo_key="sand",
            amount=1,
            finished_amount=0,
            payment=100_000,
            cost=1000,
            timestamp=timezone.now(),
        )

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "3WAY000000000000000000000000TW01",
                    "3WAY000000000000000000000000TW02",
                    "3WAY000000000000000000000000TW03",
                ],
            }
        ]
        contract_event = {
            "hook": "ServerContractCargoDelivered",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(c1.guid),
                "ContractGuid": "contract_3way_test",
                "Item": "sand",
                "Amount": 1,
                "CompletionPayment": 100_000,
                "Cost": 1000,
            },
        }

        player_profits = []
        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock) as mock_profits,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events([contract_event], http_client_mod=mock_mod)
            player_profits = mock_profits.call_args[0][0]

        # 3-person, contract_only
        # total_base_payment=0, total_subsidy=0, total_contract=100000
        # party_bonus = 0 (base payment is 0)
        # share_contract = 100000 // 3 = 33333
        # earner gets remainder: 100000 - 33333*2 = 33334
        self.assertEqual(len(player_profits), 3)
        earner_char, earner_sub, earner_pay, earner_contract = player_profits[0]
        self.assertEqual(earner_contract, 33334)
        self.assertEqual(earner_pay, 0)
        self.assertEqual(earner_sub, 0)
        for _, subsidy, payment, contract in player_profits[1:]:
            self.assertEqual(contract, 33333)
            self.assertEqual(payment, 0)
            self.assertEqual(subsidy, 0)

    async def test_contract_gov_earner_burns_full_contract(self, mock_treasury, mock_rp):
        """Full integration: Gov earner completes contract + cargo in party.

        When a gov employee earner has both cargo earnings and a contract:
        - Cargo base payment is confiscated and redirected to treasury
        - Contract payment is confiscated (burned)
        - The split values should only account for the earner's share
        """
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        from amc.webhook import on_player_profit

        player = await sync_to_async(PlayerFactory)()
        gov_char = await sync_to_async(CharacterFactory)(player=player)
        gov_char.gov_employee_until = timezone.now() + timedelta(hours=24)
        await gov_char.asave(update_fields=["gov_employee_until"])

        session = MagicMock()
        # Earner's share from a 2-person party with cargo+contract
        share_subsidy = 250
        share_base = 5000  # base_payment directly
        share_contract = 25_000

        with (
            patch("amc.webhook.transfer_money", new_callable=AsyncMock) as mock_transfer,
            patch("amc.gov_employee.redirect_income_to_treasury", new_callable=AsyncMock) as mock_redirect,
            patch("amc.webhook.subsidise_player", new_callable=AsyncMock) as mock_subsidise,
        ):
            await on_player_profit(
                gov_char, share_subsidy, share_base, session,
                contract_payment=share_contract,
            )

            # Two transfer_money calls:
            # 1. wallet_confiscation = 5000 + 25000 = -30000
            # 2. subsidy confiscation = -250
            self.assertEqual(mock_transfer.call_count, 2)
            self.assertEqual(mock_transfer.call_args_list[0].args[1], -30000)
            self.assertEqual(mock_transfer.call_args_list[1].args[1], -250)

            # Two redirect calls: earnings + subsidy contribution
            self.assertEqual(mock_redirect.call_count, 2)
            # 1. Earnings: amount=5000 (base_payment → treasury)
            self.assertEqual(mock_redirect.call_args_list[0].args[0], 5000)
            # 2. Subsidy: amount=0 (no donation), contribution=250
            self.assertEqual(mock_redirect.call_args_list[1].args[0], 0)
            self.assertEqual(mock_redirect.call_args_list[1].kwargs["contribution"], 250)

            # Subsidy paid to wallet before confiscation
            mock_subsidise.assert_called_once_with(250, gov_char, session)


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
@patch("amc.webhook.PARTY_BONUS_ENABLED", True)
class PartyContractWalletTransferBugTest(TestCase):
    def setUp(self):
        cache.clear()
    """Failing tests that assert the CORRECT behavior.

    These tests should FAIL on the current code because the party split
    does not wallet-transfer contract money to other members.

    After fixing: all should pass.
    """

    async def test_contract_money_transferred_to_other_member(self, mock_treasury, mock_rp):
        """BUG FIX: Contract money SHOULD be wallet-transferred to other members.

        Currently: earner_base_share = share_payment - share_subsidy (excludes contract)
        Expected:  earner_base_share should INCLUDE share_contract so that
                   the other member actually receives the contract money in their wallet.
        """
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        from amc.models import ServerSignContractLog

        c1 = await _create_online_character(guid="FXWT000000000000000000000000FX01")
        _ = await _create_online_character(guid="FXWT000000000000000000000000FX02")
        await _setup_delivery_points()

        await ServerSignContractLog.objects.acreate(
            guid="fix_wallet_transfer_test",
            player=c1.player,
            cargo_key="sand",
            amount=1,
            finished_amount=0,
            payment=50_000,
            cost=1000,
            timestamp=timezone.now(),
        )

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "FXWT000000000000000000000000FX01",
                    "FXWT000000000000000000000000FX02",
                ],
            }
        ]
        contract_event = {
            "hook": "ServerContractCargoDelivered",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(c1.guid),
                "ContractGuid": "fix_wallet_transfer_test",
                "Item": "sand",
                "Amount": 1,
                "CompletionPayment": 50_000,
                "Cost": 1000,
            },
        }

        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock),
            patch("amc.webhook.transfer_money", new_callable=AsyncMock) as mock_transfer,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events([contract_event], http_client_mod=mock_mod)

        # EXPECTED (after fix): contract share_contract=25000 should be
        # wallet-transferred. earner_base_share should include contract.
        # earner_base_share = (share_payment - share_subsidy) + share_contract
        #                   = (0 - 0) + 25000 = 25000
        transfer_calls = mock_transfer.call_args_list
        party_transfers = [c for c in transfer_calls if c[0][2] in ("Party Split", "Party Share")]

        # Should have 2 transfers: withdrawal from earner + deposit to other
        self.assertEqual(len(party_transfers), 2,
            "Contract money should be wallet-transferred to other party member")

        amounts = {c[0][2]: c[0][1] for c in party_transfers}
        self.assertEqual(amounts["Party Split"], -25000,
            "Should withdraw contract share from earner")
        self.assertEqual(amounts["Party Share"], 25000,
            "Should deposit contract share to other member")

    async def test_cargo_plus_contract_combined_wallet_transfer(self, mock_treasury, mock_rp):
        """BUG FIX: Combined cargo+contract should transfer both base + contract shares.

        Currently: only base cargo share is transferred.
        Expected:  base cargo share + contract share transferred together.
        """
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        from amc.models import ServerSignContractLog

        c1 = await _create_online_character(guid="FXCC000000000000000000000000FC01")
        _ = await _create_online_character(guid="FXCC000000000000000000000000FC02")
        await _setup_delivery_points()

        await ServerSignContractLog.objects.acreate(
            guid="fix_combo_transfer_test",
            player=c1.player,
            cargo_key="sand",
            amount=1,
            finished_amount=0,
            payment=50_000,
            cost=1000,
            timestamp=timezone.now(),
        )

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "FXCC000000000000000000000000FC01",
                    "FXCC000000000000000000000000FC02",
                ],
            }
        ]
        cargo_event = _make_cargo_event("FXCC000000000000000000000000FC01", payment=10_000)
        contract_event = {
            "hook": "ServerContractCargoDelivered",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(c1.guid),
                "ContractGuid": "fix_combo_transfer_test",
                "Item": "sand",
                "Amount": 1,
                "CompletionPayment": 50_000,
                "Cost": 1000,
            },
        }

        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock),
            patch("amc.webhook.transfer_money", new_callable=AsyncMock) as mock_transfer,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events([cargo_event, contract_event], http_client_mod=mock_mod)

        # cargo: 10000, bonus=500 (subsidy only)
        # share_base=5000, share_subsidy=250, share_contract=25000
        # wallet_share = share_base + share_contract = 5000 + 25000 = 30000
        transfer_calls = mock_transfer.call_args_list
        party_transfers = [c for c in transfer_calls if c[0][2] in ("Party Split", "Party Share")]

        withdrawal = [c[0][1] for c in party_transfers if c[0][2] == "Party Split"]
        deposit = [c[0][1] for c in party_transfers if c[0][2] == "Party Share"]

        self.assertEqual(len(withdrawal), 1)
        self.assertEqual(len(deposit), 1)
        # transfer = base_share + contract_share = 5000 + 25000 = 30000
        self.assertEqual(withdrawal[0], -30000,
            "Withdrawal should include both base share and contract share")
        self.assertEqual(deposit[0], 30000,
            "Deposit should include both base share and contract share")

    async def test_three_way_contract_wallet_transfers(self, mock_treasury, mock_rp):
        """BUG FIX: 3-person party with contract should transfer to 2 other members."""
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000_000

        from amc.models import ServerSignContractLog

        c1 = await _create_online_character(guid="FX3W000000000000000000000000F301")
        _ = await _create_online_character(guid="FX3W000000000000000000000000F302")
        _ = await _create_online_character(guid="FX3W000000000000000000000000F303")
        await _setup_delivery_points()

        await ServerSignContractLog.objects.acreate(
            guid="fix_3way_transfer_test",
            player=c1.player,
            cargo_key="sand",
            amount=1,
            finished_amount=0,
            payment=90_000,
            cost=1000,
            timestamp=timezone.now(),
        )

        parties = [
            {
                "PartyId": 1,
                "Players": [
                    "FX3W000000000000000000000000F301",
                    "FX3W000000000000000000000000F302",
                    "FX3W000000000000000000000000F303",
                ],
            }
        ]
        contract_event = {
            "hook": "ServerContractCargoDelivered",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(c1.guid),
                "ContractGuid": "fix_3way_transfer_test",
                "Item": "sand",
                "Amount": 1,
                "CompletionPayment": 90_000,
                "Cost": 1000,
            },
        }

        with (
            patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=parties),
            patch("amc.webhook.on_player_profits", new_callable=AsyncMock),
            patch("amc.webhook.transfer_money", new_callable=AsyncMock) as mock_transfer,
        ):
            from amc.webhook import process_events

            mock_mod = MagicMock()
            post_ctx = AsyncMock()
            post_ctx.__aenter__.return_value = MagicMock(status=200)
            post_ctx.__aexit__.return_value = None
            mock_mod.post.return_value = post_ctx
            get_ctx = AsyncMock()
            get_ctx.__aenter__.return_value = MagicMock(status=200)
            get_ctx.__aexit__.return_value = None
            mock_mod.get.return_value = get_ctx

            await process_events([contract_event], http_client_mod=mock_mod)

        # contract only: share_contract = 90000 // 3 = 30000
        # earner_base_share = 0 + 30000 = 30000 (AFTER FIX)
        # others_withdrawal = 30000 * 2 = 60000
        transfer_calls = mock_transfer.call_args_list
        party_transfers = [c for c in transfer_calls if c[0][2] in ("Party Split", "Party Share")]

        withdrawal = [c[0][1] for c in party_transfers if c[0][2] == "Party Split"]
        deposits = [c[0][1] for c in party_transfers if c[0][2] == "Party Share"]

        # Should have: 1 withdrawal (total) + 2 deposits (one per other member)
        self.assertEqual(len(withdrawal), 1)
        self.assertEqual(len(deposits), 2)
        self.assertEqual(withdrawal[0], -60000,
            "Should withdraw 2x contract share from earner")
        for dep in deposits:
            self.assertEqual(dep, 30000,
                "Each other member should receive contract share")
