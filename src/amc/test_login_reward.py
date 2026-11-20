"""Tests for the per-character daily login reward (amc.login_reward)."""

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from django.utils import timezone

from amc.login_reward import (
    BASE_REWARD,
    MAX_REWARD,
    claim_daily_login_reward,
    format_streak_clause,
    next_streak,
    reward_amount_for_streak,
)
from amc.models import Character, Player
from amc.tasks import get_welcome_message

# Async-ORM writes run outside pytest-django's per-test transaction in this
# suite, so every DB test deletes its own players (cascade removes characters).
DAY_ONE = date(2026, 11, 10)


async def _cleanup_players(*unique_ids: int) -> None:
    await Player.objects.filter(unique_id__in=list(unique_ids)).adelete()


async def _make_character(unique_id: int, **fields) -> tuple[Player, Character]:
    player = await Player.objects.acreate(unique_id=unique_id)
    character = await Character.objects.acreate(
        name=f"Login{unique_id}", player=player, guid=f"guid{unique_id}", **fields
    )
    return player, character


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------


def test_reward_amount_ramp():
    assert reward_amount_for_streak(0) == 0
    assert reward_amount_for_streak(1) == BASE_REWARD == 100_000
    assert reward_amount_for_streak(2) == 116_666
    assert reward_amount_for_streak(3) == 133_333
    assert reward_amount_for_streak(4) == 150_000
    assert reward_amount_for_streak(5) == 166_666
    assert reward_amount_for_streak(6) == 183_333
    assert reward_amount_for_streak(7) == MAX_REWARD == 200_000
    # Capped past the 7-day mark.
    assert reward_amount_for_streak(30) == MAX_REWARD


def test_next_streak_transitions():
    assert next_streak(0, None, DAY_ONE) == 1
    # Same day: unchanged (claim is refused separately).
    assert next_streak(4, DAY_ONE, DAY_ONE) == 4
    # Consecutive day: +1.
    assert next_streak(4, DAY_ONE, DAY_ONE + timedelta(days=1)) == 5
    # Missed a day: reset.
    assert next_streak(4, DAY_ONE, DAY_ONE + timedelta(days=2)) == 1
    # Clock skew (claim dated in the future): unchanged, not resettable.
    assert next_streak(4, DAY_ONE, DAY_ONE - timedelta(days=1)) == 4


def test_streak_clause_singular_and_plural():
    assert "1 day" in format_streak_clause(1, 100_000)
    assert "3 days" in format_streak_clause(3, 133_333)
    assert "$133,333" in format_streak_clause(3, 133_333)


# ---------------------------------------------------------------------------
# Claim flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_first_claim_pays_base_and_sets_day_one():
    try:
        player, character = await _make_character(910001)
        transfer = AsyncMock()
        with (
            patch("amc.login_reward.check_treasury_floor", AsyncMock(return_value=True)),
            patch("amc.login_reward.send_fund_to_player_wallet", AsyncMock()),
            patch("amc.login_reward.transfer_money", transfer),
        ):
            reward = await claim_daily_login_reward(
                character, player, MagicMockSession(), today=DAY_ONE
            )

        assert reward is not None
        assert (reward.streak, reward.amount) == (1, 100_000)
        transfer.assert_awaited_once()
        assert transfer.await_args.args[1] == 100_000
        assert transfer.await_args.args[2] == "Daily Login Reward"

        stored = await Character.objects.aget(pk=character.pk)
        assert stored.login_streak == 1
        assert stored.last_login_reward_date == DAY_ONE
    finally:
        await _cleanup_players(910001)


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_second_login_same_day_pays_nothing():
    try:
        player, character = await _make_character(910002)
        transfer = AsyncMock()
        with (
            patch("amc.login_reward.check_treasury_floor", AsyncMock(return_value=True)),
            patch("amc.login_reward.send_fund_to_player_wallet", AsyncMock()),
            patch("amc.login_reward.transfer_money", transfer),
        ):
            first = await claim_daily_login_reward(
                character, player, MagicMockSession(), today=DAY_ONE
            )
            second = await claim_daily_login_reward(
                character, player, MagicMockSession(), today=DAY_ONE
            )

        assert first is not None
        assert second is None
        assert transfer.await_count == 1
    finally:
        await _cleanup_players(910002)


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_consecutive_day_increments_payout():
    try:
        player, character = await _make_character(
            910003, login_streak=1, last_login_reward_date=DAY_ONE
        )
        transfer = AsyncMock()
        with (
            patch("amc.login_reward.check_treasury_floor", AsyncMock(return_value=True)),
            patch("amc.login_reward.send_fund_to_player_wallet", AsyncMock()),
            patch("amc.login_reward.transfer_money", transfer),
        ):
            reward = await claim_daily_login_reward(
                character, player, MagicMockSession(), today=DAY_ONE + timedelta(days=1)
            )

        assert reward is not None
        assert (reward.streak, reward.amount) == (2, 116_666)
        assert transfer.await_args.args[1] == 116_666
    finally:
        await _cleanup_players(910003)


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_missed_day_resets_streak():
    try:
        player, character = await _make_character(
            910004, login_streak=6, last_login_reward_date=DAY_ONE
        )
        with (
            patch("amc.login_reward.check_treasury_floor", AsyncMock(return_value=True)),
            patch("amc.login_reward.send_fund_to_player_wallet", AsyncMock()),
            patch("amc.login_reward.transfer_money", AsyncMock()),
        ):
            reward = await claim_daily_login_reward(
                character, player, MagicMockSession(), today=DAY_ONE + timedelta(days=2)
            )

        assert reward is not None
        assert (reward.streak, reward.amount) == (1, 100_000)
        stored = await Character.objects.aget(pk=character.pk)
        assert stored.login_streak == 1
    finally:
        await _cleanup_players(910004)


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_treasury_floor_blocks_payment():
    try:
        player, character = await _make_character(910005)
        transfer = AsyncMock()
        ledger = AsyncMock()
        with (
            patch("amc.login_reward.check_treasury_floor", AsyncMock(return_value=False)),
            patch("amc.login_reward.send_fund_to_player_wallet", ledger),
            patch("amc.login_reward.transfer_money", transfer),
        ):
            reward = await claim_daily_login_reward(
                character, player, MagicMockSession(), today=DAY_ONE
            )

        assert reward is None
        transfer.assert_not_awaited()
        ledger.assert_not_awaited()
        # The day still counts as claimed: no retry storm, no double pay.
        stored = await Character.objects.aget(pk=character.pk)
        assert stored.last_login_reward_date == DAY_ONE
    finally:
        await _cleanup_players(910005)


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_transfer_failure_does_not_raise():
    try:
        player, character = await _make_character(910006)
        with (
            patch("amc.login_reward.check_treasury_floor", AsyncMock(return_value=True)),
            patch("amc.login_reward.send_fund_to_player_wallet", AsyncMock()),
            patch(
                "amc.login_reward.transfer_money",
                AsyncMock(side_effect=Exception("Failed to transfer money")),
            ),
        ):
            reward = await claim_daily_login_reward(
                character, player, MagicMockSession(), today=DAY_ONE
            )

        assert reward is None
    finally:
        await _cleanup_players(910006)


# ---------------------------------------------------------------------------
# Welcome announcement
# ---------------------------------------------------------------------------


def test_welcome_message_includes_streak():
    message, is_new = get_welcome_message(
        "Racer",
        False,
        last_online=timezone.now() - timedelta(days=2),
        login_streak=3,
        reward_amount=133_333,
    )
    assert message is not None
    assert "Welcome back Racer!" in message
    assert "Login streak: 3 days" in message
    assert "$133,333" in message
    assert is_new is False


def test_quick_relog_still_announces_streak():
    """A second login minutes later on a NEW day still announces the streak."""
    message, _ = get_welcome_message(
        "Racer",
        False,
        last_online=timezone.now() - timedelta(minutes=20),
        login_streak=2,
        reward_amount=116_666,
    )
    assert message is not None
    assert "2 days" in message


def test_quick_relog_without_reward_stays_quiet():
    message, _ = get_welcome_message(
        "Racer", False, last_online=timezone.now() - timedelta(minutes=20)
    )
    assert message is None


def test_new_player_welcome_carries_day_one_streak():
    message, is_new = get_welcome_message(
        "Newbie", True, login_streak=1, reward_amount=100_000
    )
    assert is_new is True
    assert message is not None
    assert "Welcome Newbie!" in message
    assert "Login streak: 1 day" in message


class MagicMockSession:
    """Stand-in for the mod HTTP session; transfer_money is patched in tests."""

    def post(self, *args, **kwargs):  # pragma: no cover - never reached
        raise AssertionError("transfer_money must be patched in these tests")

