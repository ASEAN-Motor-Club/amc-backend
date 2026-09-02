"""Tests for the paid /tp2marker command and its finance helpers."""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import SimpleTestCase, TestCase

from amc.command_framework import CommandContext
from amc.commands.teleport import (
    _correct_marker_z,
    _marker_teleport_cost,
    cmd_tp2marker,
)
from amc.models import Character, Player, PoliceSession
from amc.utils import generate_verification_code
from amc_finance.loans import get_player_bank_balance
from amc_finance.models import Account
from amc_finance.services import (
    refund_player_teleport_fee,
    register_player_deposit,
    register_player_teleport_fee,
)

MARKER = {"X": 500_000, "Y": -200_000, "Z": 3_000}
PLAYER_LOC = {"X": 400_000, "Y": -200_000, "Z": 2_000}  # 1 km away


def make_ctx(character, player_info):
    ctx = MagicMock(spec=CommandContext)
    ctx.reply = AsyncMock()
    ctx.announce = AsyncMock()
    ctx.character = character
    ctx.player = character.player
    ctx.player_info = player_info
    ctx.http_client_mod = MagicMock()
    return ctx


@contextmanager
def command_patches():
    """Patch the command's external calls.

    Yields (mock_tp, mock_fee, mock_refund, mock_terrain). Bank balance is
    mocked to 5,000,000; terrain Z to 10,000.
    """
    with (
        patch(
            "amc.commands.teleport.teleport_player", new=AsyncMock()
        ) as mock_tp,
        patch(
            "amc.commands.teleport.register_player_teleport_fee", new=AsyncMock()
        ) as mock_fee,
        patch(
            "amc.commands.teleport.refund_player_teleport_fee", new=AsyncMock()
        ) as mock_refund,
        patch(
            "amc.commands.teleport.get_player_bank_balance",
            new=AsyncMock(return_value=5_000_000),
        ),
        patch(
            "amc.commands.teleport.terrain_z_cm", return_value=10_000
        ) as mock_terrain,
    ):
        yield mock_tp, mock_fee, mock_refund, mock_terrain


class MarkerTeleportCostTestCase(SimpleTestCase):
    def test_one_km_on_foot(self):
        cost, km = _marker_teleport_cost(
            {"X": 0, "Y": 0, "Z": 0}, {"X": 100_000, "Y": 0, "Z": 0}, True
        )
        self.assertEqual((cost, round(km, 6)), (15_000, 1.0))

    def test_one_km_in_vehicle_costs_double(self):
        cost, _km = _marker_teleport_cost(
            {"X": 0, "Y": 0, "Z": 0}, {"X": 100_000, "Y": 0, "Z": 0}, False
        )
        self.assertEqual(cost, 30_000)

    def test_prorates_exactly_without_minimum(self):
        cost, _km = _marker_teleport_cost(
            {"X": 0, "Y": 0, "Z": 0}, {"X": 20_000, "Y": 0, "Z": 0}, True
        )
        self.assertEqual(cost, 3_000)

    def test_short_hops_still_cost_something(self):
        cost, _km = _marker_teleport_cost(
            {"X": 0, "Y": 0, "Z": 0}, {"X": 1_000, "Y": 0, "Z": 0}, False
        )
        self.assertEqual(cost, 300)

    def test_zero_distance_costs_zero(self):
        cost, km = _marker_teleport_cost(
            {"X": 500, "Y": 700, "Z": 0}, {"X": 500, "Y": 700, "Z": 0}, True
        )
        self.assertEqual((cost, km), (0, 0.0))

    def test_vertical_offset_ignored(self):
        # 2D distance: a marker 50m up but at the same X/Y costs nothing.
        cost, km = _marker_teleport_cost(
            {"X": 0, "Y": 0, "Z": 0}, {"X": 0, "Y": 0, "Z": 5_000_000}, True
        )
        self.assertEqual((cost, km), (0, 0.0))


class CorrectMarkerZTestCase(SimpleTestCase):
    def test_raises_to_terrain_when_below(self):
        with patch("amc.commands.teleport.terrain_z_cm", return_value=10_000):
            loc = _correct_marker_z({"X": 1, "Y": 2, "Z": 300}, True)
        self.assertEqual(loc["Z"], 10_100)

    def test_keeps_game_z_when_already_above_terrain(self):
        with patch("amc.commands.teleport.terrain_z_cm", return_value=10_000):
            loc = _correct_marker_z({"X": 1, "Y": 2, "Z": 20_000}, False)
        self.assertEqual(loc["Z"], 20_005)

    def test_on_foot_offset_is_100(self):
        with patch("amc.commands.teleport.terrain_z_cm", return_value=None):
            loc = _correct_marker_z({"X": 1, "Y": 2, "Z": 500}, True)
        self.assertEqual(loc["Z"], 600)

    def test_in_vehicle_offset_is_5(self):
        with patch("amc.commands.teleport.terrain_z_cm", return_value=None):
            loc = _correct_marker_z({"X": 1, "Y": 2, "Z": 500}, False)
        self.assertEqual(loc["Z"], 505)


class Tp2MarkerCommandTestCase(TestCase):
    async def _make_character(self, name="Tp2Tester", guid="guid-tp2marker"):
        player = await Player.objects.acreate(unique_id="76561198000000042")
        return await Character.objects.acreate(name=name, player=player, guid=guid)

    @staticmethod
    def _player_info(character, **extra):
        info = {"Location": dict(PLAYER_LOC), "VehicleKey": "None"}
        info.update(extra)
        return info

    async def test_no_marker_shows_usage(self):
        character = await self._make_character()
        ctx = make_ctx(character, self._player_info(character))
        with (
            command_patches() as (mock_tp, mock_fee, _refund, _terrain),
            patch(
                "amc.commands.teleport.get_player", new=AsyncMock(return_value={})
            ),
        ):
            await cmd_tp2marker(ctx, "")
        ctx.reply.assert_awaited_once()
        self.assertIn("marker", ctx.reply.await_args[0][0].lower())
        mock_fee.assert_not_called()
        mock_tp.assert_not_called()

    async def test_quote_shows_cost_code_and_balance(self):
        character = await self._make_character()
        ctx = make_ctx(
            character,
            self._player_info(
                character, CustomDestinationAbsoluteLocation=dict(MARKER)
            ),
        )
        with command_patches():
            await cmd_tp2marker(ctx, "")
        ctx.reply.assert_awaited_once()
        msg = ctx.reply.await_args[0][0]
        self.assertIn("15,000", msg)
        self.assertIn("on foot", msg)
        self.assertIn("5,000,000", msg)
        self.assertIn("/tp2marker ", msg)
        self.assertIn("Cargo and trailers will be reset", msg)

    async def test_confirm_on_foot_charges_and_teleports(self):
        character = await self._make_character()
        ctx = make_ctx(
            character,
            self._player_info(
                character, CustomDestinationAbsoluteLocation=dict(MARKER)
            ),
        )
        code = generate_verification_code(
            (15_000, 500_000, -200_000, character.id)
        )
        with command_patches() as (mock_tp, mock_fee, mock_refund, _terrain):
            await cmd_tp2marker(ctx, code)
        mock_fee.assert_awaited_once_with(15_000, character, character.player)
        mock_tp.assert_awaited_once()
        tp_args, tp_kwargs = mock_tp.await_args
        location = tp_args[2]
        # terrain (10,000) > game Z (3,000); on foot -> +100
        self.assertEqual(location["Z"], 10_100)
        self.assertIs(tp_kwargs["no_vehicles"], False)
        self.assertIs(tp_kwargs["reset_trailers"], True)
        self.assertIs(tp_kwargs["reset_carried_vehicles"], True)
        mock_refund.assert_not_called()
        ctx.announce.assert_awaited_once()
        self.assertIn("1.0 km", ctx.announce.await_args[0][0])

    async def test_confirm_in_vehicle_uses_vehicle_rate_and_offset(self):
        character = await self._make_character()
        ctx = make_ctx(
            character,
            self._player_info(
                character,
                VehicleKey="Stinger",
                CustomDestinationAbsoluteLocation=dict(MARKER),
            ),
        )
        code = generate_verification_code(
            (30_000, 500_000, -200_000, character.id)
        )
        with command_patches() as (mock_tp, mock_fee, _refund, _terrain):
            await cmd_tp2marker(ctx, code)
        mock_fee.assert_awaited_once_with(30_000, character, character.player)
        location = mock_tp.await_args.args[2]
        self.assertEqual(location["Z"], 10_005)  # terrain + 5 (vehicle)
        self.assertIn("by vehicle", ctx.announce.await_args[0][0])

    async def test_wrong_code_only_requotes(self):
        character = await self._make_character()
        ctx = make_ctx(
            character,
            self._player_info(
                character, CustomDestinationAbsoluteLocation=dict(MARKER)
            ),
        )
        with command_patches() as (mock_tp, mock_fee, _refund, _terrain):
            await cmd_tp2marker(ctx, "WRNG")
        mock_fee.assert_not_called()
        mock_tp.assert_not_called()
        ctx.reply.assert_awaited_once()

    async def test_insufficient_balance_blocks_teleport(self):
        character = await self._make_character()
        ctx = make_ctx(
            character,
            self._player_info(
                character, CustomDestinationAbsoluteLocation=dict(MARKER)
            ),
        )
        code = generate_verification_code(
            (15_000, 500_000, -200_000, character.id)
        )
        with command_patches() as (mock_tp, mock_fee, mock_refund, _terrain):
            mock_fee.side_effect = ValueError("Unable to withdraw more than balance")
            await cmd_tp2marker(ctx, code)
        ctx.reply.assert_awaited_once()
        self.assertIn("Insufficient", ctx.reply.await_args[0][0])
        mock_tp.assert_not_called()
        mock_refund.assert_not_called()

    async def test_teleport_failure_refunds(self):
        character = await self._make_character()
        ctx = make_ctx(
            character,
            self._player_info(
                character, CustomDestinationAbsoluteLocation=dict(MARKER)
            ),
        )
        code = generate_verification_code(
            (15_000, 500_000, -200_000, character.id)
        )
        with command_patches() as (mock_tp, _fee, mock_refund, _terrain):
            mock_tp.side_effect = Exception("mod server 400")
            await cmd_tp2marker(ctx, code)
        mock_refund.assert_awaited_once_with(15_000, character, character.player)
        ctx.reply.assert_awaited_once()
        self.assertIn("refunded", ctx.reply.await_args[0][0])
        ctx.announce.assert_not_called()

    async def test_rp_mode_refused(self):
        character = await Character.objects.acreate(
            name="RpTester",
            player=await Player.objects.acreate(unique_id="76561198000000044"),
            guid="guid-rp",
            rp_mode=True,
        )
        ctx = make_ctx(character, self._player_info(character))
        with command_patches() as (mock_tp, mock_fee, _refund, _terrain):
            await cmd_tp2marker(ctx, "")
        ctx.reply.assert_awaited_once()
        self.assertIn("RP mode", ctx.reply.await_args[0][0])
        mock_fee.assert_not_called()
        mock_tp.assert_not_called()

    async def test_police_on_duty_refused(self):
        character = await self._make_character()
        await PoliceSession.objects.acreate(character=character)
        ctx = make_ctx(character, self._player_info(character))
        with command_patches() as (mock_tp, mock_fee, _refund, _terrain):
            await cmd_tp2marker(ctx, "")
        ctx.reply.assert_awaited_once()
        self.assertIn("police duty", ctx.reply.await_args[0][0])
        mock_fee.assert_not_called()
        mock_tp.assert_not_called()

    async def test_zero_cost_skips_ledger(self):
        character = await self._make_character()
        marker = {"X": 400_000, "Y": -200_000, "Z": 2_000}
        ctx = make_ctx(
            character,
            self._player_info(
                character, CustomDestinationAbsoluteLocation=dict(marker)
            ),
        )
        code = generate_verification_code((0, 400_000, -200_000, character.id))
        with command_patches() as (mock_tp, mock_fee, mock_refund, _terrain):
            await cmd_tp2marker(ctx, "")  # first call: quote
            await cmd_tp2marker(ctx, code)  # confirm
        mock_fee.assert_not_called()
        mock_refund.assert_not_called()
        mock_tp.assert_awaited_once()
        ctx.announce.assert_awaited_once()
        self.assertIn("for 0", ctx.announce.await_args[0][0])

    async def test_marker_fetched_from_mod_when_missing(self):
        character = await self._make_character()
        ctx = make_ctx(character, self._player_info(character))
        mod_player = {"CustomDestinationAbsoluteLocation": dict(MARKER)}
        with (
            command_patches(),
            patch(
                "amc.commands.teleport.get_player",
                new=AsyncMock(return_value=mod_player),
            ) as mock_get_player,
        ):
            await cmd_tp2marker(ctx, "")
        mock_get_player.assert_awaited_once()
        # Quote shows the marker-based cost
        self.assertIn("15,000", ctx.reply.await_args[0][0])


class TeleportFeeLedgerTestCase(TestCase):
    """Finance-layer test: real postings against the real account graph."""

    async def _make_character(self):
        player = await Player.objects.acreate(unique_id="76561198000000043")
        return await Character.objects.acreate(
            name="LedgerTester", player=player, guid="guid-ledger"
        )

    @staticmethod
    async def _account(name):
        return await Account.objects.aget(name=name)

    async def test_fee_and_refund_postings(self):
        player = await Player.objects.acreate(unique_id="76561198000000043")
        character = await Character.objects.acreate(
            name="LedgerTester", player=player, guid="guid-ledger"
        )

        # Fund the checking account.
        await register_player_deposit(100_000, character, player)
        self.assertEqual(await get_player_bank_balance(character), 100_000)

        # Charge.
        await register_player_teleport_fee(15_000, character, player)
        self.assertEqual(await get_player_bank_balance(character), 85_000)
        revenue = await self._account("Treasury Revenue")
        fund = await self._account("Treasury Fund")
        self.assertEqual(revenue.balance, 15_000)
        self.assertEqual(fund.balance, 15_000)
        await character.arefresh_from_db()
        self.assertEqual(character.total_donations, 0)

        # Refund reverses everything exactly.
        await refund_player_teleport_fee(15_000, character, player)
        self.assertEqual(await get_player_bank_balance(character), 100_000)
        revenue = await self._account("Treasury Revenue")
        fund = await self._account("Treasury Fund")
        self.assertEqual(revenue.balance, 0)
        self.assertEqual(fund.balance, 0)

    async def test_fee_over_balance_raises_without_posting(self):
        character = await self._make_character()
        with self.assertRaises(ValueError):
            await register_player_teleport_fee(15_000, character, character.player)
        # Nothing was posted: no treasury accounts were even created.
        self.assertFalse(
            await Account.objects.filter(name="Treasury Revenue").aexists()
        )
        self.assertEqual(await get_player_bank_balance(character), 0)
