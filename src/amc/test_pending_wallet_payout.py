"""Tests for pending wallet payouts (bank -> wallet at login)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async

from amc.factories import CharacterFactory, PlayerFactory
from amc.models import PendingWalletPayout
from amc.pending_payout import deliver_pending_wallet_payouts
from amc_finance.models import Account


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_delivery_books_withdrawal_and_marks_paid():
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
    # Fund the checking account so the withdrawal passes.
    from amc_finance.services import register_player_deposit

    await register_player_deposit(1_000_000, character, player)
    account = await Account.objects.aget(
        character=character,
        account_type=Account.AccountType.LIABILITY,
        book=Account.Book.BANK,
        name="Checking Account",
    )
    payout = await PendingWalletPayout.objects.acreate(
        player=player, amount=400_000, reason="Fraud clawback correction"
    )

    http_client_mod = MagicMock()
    http_client_mod.post = AsyncMock()

    with (
        patch(
            "amc.pending_payout.transfer_money", new_callable=AsyncMock
        ) as tm,
        patch("amc.pending_payout.show_popup", new_callable=AsyncMock),
    ):
        await deliver_pending_wallet_payouts(
            player, character, http_client_mod
        )

    tm.assert_awaited_once()
    await payout.arefresh_from_db()
    assert payout.booked_at is not None
    assert payout.paid_at is not None
    # Checking account debited by the payout amount.
    await account.arefresh_from_db()
    assert account.balance == 600_000


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_insufficient_balance_leaves_unpaid():
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
    payout = await PendingWalletPayout.objects.acreate(
        player=player, amount=5_000_000, reason="Fraud clawback correction"
    )

    http_client_mod = MagicMock()
    http_client_mod.post = AsyncMock()

    with (
        patch(
            "amc.pending_payout.transfer_money", new_callable=AsyncMock
        ) as tm,
        patch("amc.pending_payout.show_popup", new_callable=AsyncMock),
    ):
        await deliver_pending_wallet_payouts(
            player, character, http_client_mod
        )

    tm.assert_not_awaited()
    await payout.arefresh_from_db()
    assert payout.booked_at is None
    assert payout.paid_at is None


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_transfer_failure_keeps_booked_and_retries_only_wallet_leg():
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
    from amc_finance.services import register_player_deposit

    await register_player_deposit(1_000_000, character, player)
    account = await Account.objects.aget(
        character=character,
        account_type=Account.AccountType.LIABILITY,
        book=Account.Book.BANK,
        name="Checking Account",
    )
    payout = await PendingWalletPayout.objects.acreate(
        player=player, amount=400_000, reason="Fraud clawback correction"
    )
    http_client_mod = MagicMock()
    http_client_mod.post = AsyncMock()

    # First attempt: wallet transfer fails after the ledger leg is booked.
    with (
        patch(
            "amc.pending_payout.transfer_money",
            new_callable=AsyncMock,
            side_effect=Exception("mod down"),
        ),
        patch("amc.pending_payout.show_popup", new_callable=AsyncMock),
    ):
        await deliver_pending_wallet_payouts(
            player, character, http_client_mod
        )

    await payout.arefresh_from_db()
    assert payout.booked_at is not None
    assert payout.paid_at is None
    await account.arefresh_from_db()
    assert account.balance == 600_000  # withdrawn exactly once

    # Retry: only the wallet leg runs; no second withdrawal.
    with (
        patch(
            "amc.pending_payout.transfer_money", new_callable=AsyncMock
        ) as tm,
        patch("amc.pending_payout.show_popup", new_callable=AsyncMock),
    ):
        await deliver_pending_wallet_payouts(
            player, character, http_client_mod
        )

    tm.assert_awaited_once()
    await payout.arefresh_from_db()
    assert payout.paid_at is not None
    await account.arefresh_from_db()
    assert account.balance == 600_000  # still debited only once
