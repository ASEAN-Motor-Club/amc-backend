"""Tests for the daily Active Discord role sync (amc.active_role)."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from django.utils import timezone

from amc.active_role import (
    compute_role_changes,
    get_active_discord_ids,
)
from amc.models import Character, Player, PlayerStatusLog

# ---------------------------------------------------------------------------
# get_active_discord_ids — DB target set
#
# NOTE: async-ORM writes (acreate) run on a threadpool connection OUTSIDE
# pytest-django's per-test transaction, so rows leak between tests in this
# suite. Assertions are scoped to each test's own IDs (suite convention) —
# never assert absolute emptiness of a shared table.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_get_active_discord_ids_linked_recent_and_multichar():
    now = timezone.now()

    linked_active = await Player.objects.acreate(unique_id=1, discord_user_id=1001)
    linked_stale = await Player.objects.acreate(unique_id=2, discord_user_id=1002)
    unlinked_active = await Player.objects.acreate(unique_id=3)  # no discord_user_id
    await Player.objects.acreate(unique_id=4, discord_user_id=1004)  # never logged in

    for player, age in ((linked_active, 1), (linked_stale, 45), (unlinked_active, 1)):
        character = await Character.objects.acreate(
            player=player, name=f"c{player.unique_id}"
        )
        await PlayerStatusLog.objects.acreate(
            character=character, timespan=(now - timedelta(days=age), None)
        )
    # multi-character player: newest character login counts (stale char + fresh one)
    alt = await Character.objects.acreate(player=linked_stale, name="c2-alt")
    await PlayerStatusLog.objects.acreate(
        character=alt, timespan=(now - timedelta(days=2), None)
    )

    result = await get_active_discord_ids(window_days=30)
    assert {1001, 1002} <= result  # fresh login; rescued by newest-char login
    assert 1004 not in result  # linked but never logged in


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_get_active_discord_ids_window_boundary():
    """A login just past the cutoff is out; just inside the cutoff is in."""
    now = timezone.now()
    player = await Player.objects.acreate(unique_id=10, discord_user_id=1010)
    character = await Character.objects.acreate(player=player, name="c10")
    await PlayerStatusLog.objects.acreate(
        character=character,
        timespan=(now - timedelta(days=30, seconds=1), None),
    )
    result = await get_active_discord_ids(window_days=30)
    assert 1010 not in result

    await PlayerStatusLog.objects.acreate(
        character=character, timespan=(now - timedelta(days=29, hours=23), None)
    )
    assert 1010 in await get_active_discord_ids(window_days=30)


# ---------------------------------------------------------------------------
# compute_role_changes — pure diff
# ---------------------------------------------------------------------------


def test_compute_role_changes_diffs_both_directions():
    to_add, to_remove = compute_role_changes(
        active_ids={1, 2, 3}, member_ids_with_role={2, 3, 99}
    )
    assert to_add == [1]
    assert to_remove == [99]


def test_compute_role_changes_empty_sides():
    assert compute_role_changes(set(), {5}) == ([], [5])
    assert compute_role_changes({5}, set()) == ([5], [])
    assert compute_role_changes(set(), set()) == ([], [])
