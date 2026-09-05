"""Negative / non-positive amount hardening for player finance commands.

Audit (2026-09-03) found that /donate, /withdraw and /loan accepted negative
amounts. Because create_journal_entry() drops non-positive legs, the ledger
stayed untouched, but:

- /donate -X decremented Character.total_donations by X (stats corruption)
  and announced a negative donation.
- /withdraw -X and /loan -X called transfer_money() with the negative amount,
  silently destroying wallet money with no ledger trace.

These tests pin the desired behaviour: commands reject non-positive and
non-numeric amounts with a friendly popup, and the finance service primitives
refuse non-positive amounts.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase

from amc.command_framework import CommandContext
from amc.commands.finance import cmd_burn, cmd_donate, cmd_loan, cmd_withdraw
from amc.models import Character, Player
from amc.utils import generate_verification_code
from amc_finance.loans import (
    get_player_bank_balance,
    get_player_loan_balance,
    register_player_take_loan,
)
from amc_finance.models import LedgerEntry
from amc_finance.services import player_donation, register_player_withdrawal


class MockResponse:
    def __init__(self, data=None):
        self.status = 200
        self.data = data or {}
        self.json = AsyncMock(return_value=self.data)

    def __await__(self):
        return self._await().__await__()

    async def _await(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def make_ctx():
    ctx = MagicMock(spec=CommandContext)
    ctx.reply = AsyncMock()
    ctx.announce = AsyncMock()
    ctx.http_client_mod = MagicMock()
    ctx.http_client = MagicMock()
    ctx.discord_client = None
    ctx.http_client_mod.get.return_value = MockResponse()
    ctx.http_client_mod.post.return_value = MockResponse()
    ctx.http_client.get.return_value = MockResponse()
    ctx.http_client.post.return_value = MockResponse()
    return ctx


class NegativeAmountServiceTestCase(TestCase):
    async def _make_character(self) -> Character:
        player = await sync_to_async(Player.objects.create)(
            unique_id="76561198000000001"
        )
        return await sync_to_async(Character.objects.create)(
            name="NegServiceChar", player=player, guid="guid-neg-svc"
        )

    async def test_withdrawal_negative_raises(self):
        character = await self._make_character()
        player = await sync_to_async(lambda: character.player)()
        with self.assertRaises(ValueError):
            await register_player_withdrawal(-100, character, player)
        self.assertEqual(await get_player_bank_balance(character), 0)
        # No ledger legs were created for this character
        count = await LedgerEntry.objects.filter(
            account__character=character
        ).acount()
        self.assertEqual(count, 0)

    async def test_withdrawal_zero_raises(self):
        character = await self._make_character()
        player = await sync_to_async(lambda: character.player)()
        with self.assertRaises(ValueError):
            await register_player_withdrawal(0, character, player)

    async def test_take_loan_negative_raises(self):
        character = await self._make_character()
        with self.assertRaises(ValueError):
            await register_player_take_loan(-100, character)
        self.assertEqual(await get_player_loan_balance(character), 0)

    async def test_donation_negative_is_noop(self):
        character = await self._make_character()
        await player_donation(-100, character)
        await character.arefresh_from_db()
        self.assertEqual(character.total_donations, 0)

    async def test_donation_zero_is_noop(self):
        # The gov-employee pipeline (profit.py) calls player_donation(0, ...)
        # for subsidy-only contributions — must stay a harmless no-op.
        character = await self._make_character()
        await player_donation(0, character)
        await character.arefresh_from_db()
        self.assertEqual(character.total_donations, 0)


class NegativeAmountCommandTestCase(TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.player = Player.objects.create(unique_id="76561198000000002")
        self.character = Character.objects.create(
            name="NegTestChar", player=self.player, guid="guid-neg"
        )
        self.ctx.character = self.character
        self.ctx.player = self.player

    async def test_cmd_donate_negative_rejected(self):
        # Even with a (previously valid) confirmation code, a negative amount
        # must never reach the ledger or the donation stats.
        code = generate_verification_code((-500, self.character.id))
        with (
            patch(
                "amc.commands.finance.register_player_withdrawal", new=AsyncMock()
            ) as mock_withdraw,
            patch(
                "amc.commands.finance.player_donation", new=AsyncMock()
            ) as mock_donate,
        ):
            await cmd_donate(self.ctx, "-500", code)
            mock_withdraw.assert_not_called()
            mock_donate.assert_not_called()
        self.ctx.reply.assert_called()

    async def test_cmd_donate_nonnumeric_rejected(self):
        with patch(
            "amc.commands.finance.register_player_withdrawal", new=AsyncMock()
        ) as mock_withdraw:
            await cmd_donate(self.ctx, "abc", "")
            mock_withdraw.assert_not_called()
        self.ctx.reply.assert_called()

    async def test_cmd_withdraw_negative_rejected(self):
        with (
            patch(
                "amc.commands.finance.register_player_withdrawal", new=AsyncMock()
            ) as mock_withdraw,
            patch(
                "amc.commands.finance.transfer_money", new=AsyncMock()
            ) as mock_transfer,
        ):
            await cmd_withdraw(self.ctx, "-500", "")
            mock_withdraw.assert_not_called()
            mock_transfer.assert_not_called()
        self.ctx.reply.assert_called()

    async def test_cmd_loan_negative_rejected(self):
        stub_delivery = MagicMock()
        stub_delivery.objects.filter.return_value.aexists = AsyncMock(
            return_value=True
        )
        with (
            patch("amc.commands.finance.Delivery", stub_delivery),
            patch(
                "amc.commands.finance.register_player_take_loan", new=AsyncMock()
            ) as mock_take_loan,
            patch(
                "amc.commands.finance.transfer_money", new=AsyncMock()
            ) as mock_transfer,
        ):
            await cmd_loan(self.ctx, "-500", "")
            mock_take_loan.assert_not_called()
            mock_transfer.assert_not_called()
        self.ctx.reply.assert_called()

    async def test_cmd_burn_negative_rejected(self):
        code = generate_verification_code((-500, self.character.id))
        with patch(
            "amc.commands.finance.transfer_money", new=AsyncMock()
        ) as mock_transfer:
            await cmd_burn(self.ctx, "-500", code)
            mock_transfer.assert_not_called()
        self.ctx.reply.assert_called()
