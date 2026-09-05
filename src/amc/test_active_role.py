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


# NOTE: async-ORM writes (acreate) run on a threadpool connection OUTSIDE
# pytest-django's per-test transaction, so rows written here PERSIST past the
# test (they are not rolled back) and would pollute downstream test files
# (hit 2026-09-03: leaked active-looking players broke test_elections and
# test_leaderboard_cog). Every DB test therefore deletes its own players in a
# try/finally — Player deletion cascades to characters and status logs.
# ---------------------------------------------------------------------------


async def _cleanup_players(*unique_ids: int) -> None:
    await Player.objects.filter(unique_id__in=list(unique_ids)).adelete()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_get_active_discord_ids_linked_recent_and_multichar():
    try:
        now = timezone.now()

        linked_active = await Player.objects.acreate(unique_id=1, discord_user_id=1001)
        linked_stale = await Player.objects.acreate(unique_id=2, discord_user_id=1002)
        unlinked_active = await Player.objects.acreate(
            unique_id=3
        )  # no discord_user_id
        await Player.objects.acreate(
            unique_id=4, discord_user_id=1004
        )  # never logged in

        for player, age in (
            (linked_active, 1),
            (linked_stale, 45),
            (unlinked_active, 1),
        ):
            character = await Character.objects.acreate(
                player=player, name=f"c{player.unique_id}"
            )
            await PlayerStatusLog.objects.acreate(
                character=character, timespan=(now - timedelta(days=age), None)
            )
        # multi-character player: newest character login counts (stale + fresh)
        alt = await Character.objects.acreate(player=linked_stale, name="c2-alt")
        await PlayerStatusLog.objects.acreate(
            character=alt, timespan=(now - timedelta(days=2), None)
        )

        result = await get_active_discord_ids(window_days=30)
        assert {1001, 1002} <= result  # fresh login; rescued by newest-char login
        assert 1004 not in result  # linked but never logged in
    finally:
        await _cleanup_players(1, 2, 3, 4)


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_get_active_discord_ids_window_boundary():
    """A login just past the cutoff is out; just inside the cutoff is in."""
    try:
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
    finally:
        await _cleanup_players(10)


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


# ---------------------------------------------------------------------------
# sync_active_role — mocked-bot integration
# ---------------------------------------------------------------------------


def _member(uid: int) -> MagicMock:
    m = MagicMock()
    m.id = uid
    m.add_roles = AsyncMock()
    m.remove_roles = AsyncMock()
    return m


def _bot(role_members, guild_members):
    role = MagicMock()
    role.members = role_members
    guild = MagicMock()
    guild.get_role.return_value = role
    guild.get_member = lambda uid: next((m for m in guild_members if m.id == uid), None)
    # Unknown members (e.g. rows leaked between tests) raise NotFound like a
    # real guild where the user never joined.
    import discord

    guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(), "x"))
    bot = MagicMock()
    bot.get_guild.return_value = guild
    return bot, guild, role


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_sync_adds_and_removes(settings):
    from amc.active_role import sync_active_role

    settings.DISCORD_ACTIVE_ROLE_ID = 555
    settings.DISCORD_GUILD_ID = 42
    now = timezone.now()

    try:
        active_p = await Player.objects.acreate(unique_id=21, discord_user_id=21)
        active_c = await Character.objects.acreate(player=active_p, name="c21")
        await PlayerStatusLog.objects.acreate(character=active_c, timespan=(now, None))
        # linked but no recent login → must lose the role
        await Player.objects.acreate(unique_id=22, discord_user_id=22)

        stale_member = _member(22)  # currently holds the role, inactive
        active_member = _member(21)  # active, no role yet
        bot, _guild, _role = _bot(
            role_members=[stale_member],
            guild_members=[active_member, stale_member],
        )

        summary = await sync_active_role(bot)

        active_member.add_roles.assert_awaited_once()
        stale_member.remove_roles.assert_awaited_once()
        # exact added/removed counts are not asserted: rows leaked between tests
        # (async-ORM writes bypass the per-test transaction) may add to the diff.
        assert summary["skipped"] is False
        assert summary["added"] >= 1
        assert summary["removed"] >= 1
    finally:
        await _cleanup_players(21, 22)


@pytest.mark.asyncio
async def test_sync_skips_when_role_id_unset(settings):
    from amc.active_role import sync_active_role

    settings.DISCORD_ACTIVE_ROLE_ID = 0
    bot, _guild, _role = _bot(role_members=[], guild_members=[])
    summary = await sync_active_role(bot)
    assert summary == {"skipped": True, "added": 0, "removed": 0, "missing": 0}
    bot.get_guild.assert_not_called()


@pytest.mark.asyncio
async def test_sync_skips_when_role_not_found(settings):
    from amc.active_role import sync_active_role

    settings.DISCORD_ACTIVE_ROLE_ID = 555
    settings.DISCORD_GUILD_ID = 42
    bot, guild, _role = _bot(role_members=[], guild_members=[])
    guild.get_role.return_value = None
    summary = await sync_active_role(bot)
    assert summary["skipped"] is True


@pytest.mark.asyncio
async def test_sync_casts_guild_id_to_int(settings):
    """discord.py's guild store is int-keyed (state.py:302) — the raw str
    from os.environ must be cast before get_guild, else the lookup returns
    None forever (found in the 2026-09-04 review). Set the PRODUCTION shape
    (str) and pin the int reaching the bot."""
    from amc.active_role import sync_active_role

    settings.DISCORD_ACTIVE_ROLE_ID = 555
    settings.DISCORD_GUILD_ID = "1535855262739603526"  # str, as env vars arrive
    bot, guild, _role = _bot(role_members=[], guild_members=[])
    guild.get_role.return_value = None  # early-return: no DB touch needed

    summary = await sync_active_role(bot)

    bot.get_guild.assert_called_once_with(1535855262739603526)
    assert summary["skipped"] is True


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_sync_counts_missing_member_without_raising(settings):
    import discord

    from amc.active_role import sync_active_role

    settings.DISCORD_ACTIVE_ROLE_ID = 555
    settings.DISCORD_GUILD_ID = 42
    now = timezone.now()
    try:
        player = await Player.objects.acreate(unique_id=31, discord_user_id=31)
        character = await Character.objects.acreate(player=player, name="c31")
        await PlayerStatusLog.objects.acreate(character=character, timespan=(now, None))

        bot, guild, _role = _bot(role_members=[], guild_members=[])
        guild.get_member = lambda uid: None
        guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(), "x"))

        summary = await sync_active_role(bot)
        assert summary["skipped"] is False
        assert summary["missing"] >= 1  # own player (31) is not in the guild
    finally:
        await _cleanup_players(31)


@pytest.mark.asyncio
async def test_cron_wrapper_skips_when_client_not_ready():
    from types import SimpleNamespace

    from amc import active_role

    ctx = {"discord_client": SimpleNamespace(is_ready=lambda: False)}
    # must return cleanly, never raise, never touch discord
    assert await active_role.active_role_cron(ctx) is None
