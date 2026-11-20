"""Daily login rewards — one claim per character per calendar day.

The reward is 100,000 on the first claim and grows linearly with the
consecutive-day login streak, reaching 200,000 at a 7-day streak and staying
capped there. A missed calendar day resets the streak to 1.

Payout follows the canonical treasury-funded shape used by UBI/subsidies:
``check_treasury_floor`` -> ``send_fund_to_player_wallet`` (the ledger leg) ->
``transfer_money`` (the real in-game wallet). Nothing is paid when the floor
check fails or the transfer errors.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.utils import timezone

from amc.mod_server import transfer_money
from amc.models import Character
from amc_finance.services import check_treasury_floor, send_fund_to_player_wallet

logger = logging.getLogger("amc.login_reward")

BASE_REWARD = 100_000
MAX_REWARD = 200_000
# Streak length at which the reward reaches MAX_REWARD; longer streaks stay
# capped (a 30-day streak is not 3.4x a 7-day one).
MAX_STREAK_DAYS = 7
REWARD_DESCRIPTION = "Daily Login Reward"


@dataclass(frozen=True)
class LoginReward:
    streak: int
    amount: int


def reward_amount_for_streak(streak: int) -> int:
    """Linear ramp from BASE_REWARD (streak 1) to MAX_REWARD (streak 7+)."""
    if streak <= 0:
        return 0
    day = min(streak, MAX_STREAK_DAYS)
    span = MAX_REWARD - BASE_REWARD
    return BASE_REWARD + span * (day - 1) // (MAX_STREAK_DAYS - 1)


def next_streak(
    previous_streak: int, last_reward_date: date | None, today: date
) -> int:
    """Streak value for a claim on *today*.

    Same day -> unchanged (the caller should already have refused the claim);
    yesterday -> +1; any earlier day (or no history) -> back to 1.
    """
    if last_reward_date is None:
        return 1
    gap = (today - last_reward_date).days
    if gap <= 0:
        return previous_streak
    if gap == 1:
        return previous_streak + 1
    return 1


async def claim_daily_login_reward(
    character: Character,
    player,
    http_client_mod,
    today: date | None = None,
) -> LoginReward | None:
    """Claim today's reward for *character*; None when already claimed.

    The claim is marked in the DB *before* paying so a concurrent login (or a
    retried log line) cannot pay twice: the conditional UPDATE only matches a
    row whose ``last_login_reward_date`` is not already today.
    """
    today = today or timezone.localdate()
    streak = next_streak(character.login_streak or 0, character.last_login_reward_date, today)
    amount = reward_amount_for_streak(streak)

    claimed = (
        await Character.objects.filter(pk=character.pk)
        .exclude(last_login_reward_date=today)
        .aupdate(login_streak=streak, last_login_reward_date=today)
    )
    if not claimed:
        return None

    character.login_streak = streak
    character.last_login_reward_date = today

    if amount <= 0:
        return None

    try:
        if not await check_treasury_floor(Decimal(amount)):
            logger.warning(
                "Daily login reward skipped for %s (%s): treasury below floor",
                character.name,
                player.unique_id,
            )
            return None
        await send_fund_to_player_wallet(Decimal(amount), character, REWARD_DESCRIPTION)
        await transfer_money(http_client_mod, amount, REWARD_DESCRIPTION, player.unique_id)
    except Exception:
        logger.exception(
            "Daily login reward payment failed for %s (%s)",
            character.name,
            player.unique_id,
        )
        return None

    logger.info(
        "Daily login reward: %s (%s) day %d streak, $%s",
        character.name,
        player.unique_id,
        streak,
        f"{amount:,}",
    )
    return LoginReward(streak=streak, amount=amount)


def format_streak_clause(streak: int, amount: int) -> str:
    """Short chat-safe clause appended to the welcome announcement."""
    days = "day" if streak == 1 else "days"
    return f"Login streak: {streak} {days}. Daily reward: ${amount:,}."
