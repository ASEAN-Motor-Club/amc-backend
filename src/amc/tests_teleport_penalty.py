import time
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase
from django.utils import timezone

from amc.factories import PlayerFactory, CharacterFactory
from amc.models import Confiscation, Delivery, PoliceSession
from amc.webhook import process_events


def _teleport_event(character_guid, hook="ServerTeleportCharacter", seq=100):
    """Build a teleport event with proper dedup fields."""
    return {
        "hook": hook,
        "timestamp": int(time.time()),
        "_seq": seq,
        "_epoch": "test-epoch",
        "data": {
            "CharacterGuid": str(character_guid),
            "AbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
        },
    }


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock, return_value=False)
@patch("amc.webhook.get_parties", new_callable=AsyncMock, return_value=[])
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock, return_value=100_000)
@patch("amc.player_tags.refresh_player_name", new_callable=AsyncMock)
@patch("amc.webhook.transfer_money", new_callable=AsyncMock)
@patch("amc.webhook.show_popup", new_callable=AsyncMock)
@patch("amc.webhook.announce", new_callable=AsyncMock)
class TeleportPenaltyTests(TestCase):
    """Tests for handle_teleport_or_respawn — penalty for criminals who teleport."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    async def _setup_character(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        return player, character

    async def _deliver_money(self, character, payment=100_000, minutes_ago=0):
        """Create a Money Delivery record as if the character just delivered."""
        ts = timezone.now() - timedelta(minutes=minutes_ago)
        return await Delivery.objects.acreate(
            timestamp=ts,
            character=character,
            cargo_key="Money",
            quantity=1,
            payment=payment,
        )

    async def _process_teleport(self, character, hook="ServerTeleportCharacter", seq=100):
        """Run a single teleport event through the full process_events pipeline."""
        events = [_teleport_event(character.guid, hook=hook, seq=seq)]
        http_client = AsyncMock()
        http_client_mod = AsyncMock()
        await process_events(events, http_client=http_client, http_client_mod=http_client_mod)

    async def test_teleport_immediately_after_delivery_full_penalty(
        self, mock_announce, mock_popup, mock_transfer, mock_refresh,
        mock_treasury, mock_parties, mock_rp,
    ):
        """Teleporting immediately after delivery → ~100% penalty."""
        _, character = await self._setup_character()
        await self._deliver_money(character, payment=100_000, minutes_ago=0)

        await self._process_teleport(character)

        # Should deduct full payment (minor variance from test timing)
        mock_transfer.assert_called_once()
        args = mock_transfer.call_args
        self.assertAlmostEqual(args[0][1], -100_000, delta=500)
        self.assertEqual(args[0][2], "Teleport Penalty")

        # criminal_laundered_total should be 0
        await character.arefresh_from_db()
        self.assertEqual(character.criminal_laundered_total, 0)

        # Confiscation record created (officer=None for self-inflicted)
        conf = await Confiscation.objects.filter(character=character).afirst()
        self.assertIsNotNone(conf)
        self.assertIsNone(conf.officer)
        self.assertAlmostEqual(conf.amount, 100_000, delta=500)

    async def test_teleport_after_half_window_linear_decay(
        self, mock_announce, mock_popup, mock_transfer, mock_refresh,
        mock_treasury, mock_parties, mock_rp,
    ):
        """Teleporting 5 minutes after delivery → 50% penalty."""
        _, character = await self._setup_character()
        await self._deliver_money(character, payment=100_000, minutes_ago=5)

        await self._process_teleport(character, hook="ServerTeleportVehicle")

        args = mock_transfer.call_args
        penalty = abs(args[0][1])
        # rate = 1 - 5/10 = 0.5 → 50_000
        self.assertAlmostEqual(penalty, 50_000, delta=1_000)

    async def test_teleport_after_window_no_penalty(
        self, mock_announce, mock_popup, mock_transfer, mock_refresh,
        mock_treasury, mock_parties, mock_rp,
    ):
        """Teleporting 11 minutes after delivery → no penalty."""
        _, character = await self._setup_character()
        await self._deliver_money(character, payment=100_000, minutes_ago=11)

        await self._process_teleport(character, hook="ServerRespawnCharacter")

        mock_transfer.assert_not_called()
        self.assertEqual(
            await Confiscation.objects.filter(character=character).acount(), 0
        )

    async def test_non_money_delivery_no_penalty(
        self, mock_announce, mock_popup, mock_transfer, mock_refresh,
        mock_treasury, mock_parties, mock_rp,
    ):
        """Teleporting after non-Money delivery → no penalty."""
        _, character = await self._setup_character()
        await Delivery.objects.acreate(
            timestamp=timezone.now(),
            character=character,
            cargo_key="oranges",
            quantity=1,
            payment=100_000,
        )

        await self._process_teleport(character)

        mock_transfer.assert_not_called()

    async def test_police_officer_not_penalised(
        self, mock_announce, mock_popup, mock_transfer, mock_refresh,
        mock_treasury, mock_parties, mock_rp,
    ):
        """Active police officers are not penalised."""
        _, character = await self._setup_character()
        await self._deliver_money(character, payment=100_000, minutes_ago=0)
        await PoliceSession.objects.acreate(character=character)

        await self._process_teleport(character)

        mock_transfer.assert_not_called()

    async def test_multiple_deliveries_summed(
        self, mock_announce, mock_popup, mock_transfer, mock_refresh,
        mock_treasury, mock_parties, mock_rp,
    ):
        """Multiple recent deliveries are summed with per-delivery decay."""
        _, character = await self._setup_character()
        # Delivery 1: 2 min ago → rate = 0.8 → 80_000 penalty
        await self._deliver_money(character, payment=100_000, minutes_ago=2)
        # Delivery 2: 8 min ago → rate = 0.2 → 10_000 penalty
        await self._deliver_money(character, payment=50_000, minutes_ago=8)

        # Set criminal_laundered_total to match deliveries
        character.criminal_laundered_total = 150_000
        await character.asave(update_fields=["criminal_laundered_total"])

        await self._process_teleport(character)

        args = mock_transfer.call_args
        penalty = abs(args[0][1])
        # 100_000 * 0.8 + 50_000 * 0.2 = 80_000 + 10_000 = 90_000
        self.assertAlmostEqual(penalty, 90_000, delta=2_000)

        # criminal_laundered_total should be reduced
        await character.arefresh_from_db()
        self.assertAlmostEqual(
            character.criminal_laundered_total, 60_000, delta=2_000
        )
