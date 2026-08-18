import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from asgiref.sync import sync_to_async
from datetime import timedelta

from django.utils import timezone

from amc.mute import (
    PERMANENT_MUTE_UNTIL,
    persist_mute,
    clear_persistent_mute,
    reapply_mute_on_login,
)


@pytest.fixture(autouse=True)
def clear_cache():
    from django.core.cache import cache

    cache.clear()


# --- persist_mute / clear_persistent_mute ---


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_persist_permanent_mute_uses_sentinel():
    from amc.factories import PlayerFactory

    player = await sync_to_async(PlayerFactory)()
    await persist_mute(player, True)
    await player.arefresh_from_db()
    assert player.muted_until is not None
    assert player.muted_until.year >= 9999


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_persist_temp_mute_is_absolute_future():
    from amc.factories import PlayerFactory

    player = await sync_to_async(PlayerFactory)()
    await persist_mute(player, 600)
    await player.arefresh_from_db()
    assert player.muted_until is not None
    assert player.muted_until.year < 9999
    remaining = player.muted_until - timezone.now()
    assert timedelta(seconds=580) < remaining < timedelta(seconds=620)


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_clear_persistent_mute_resets():
    from amc.factories import PlayerFactory

    player = await sync_to_async(PlayerFactory)(muted_until=PERMANENT_MUTE_UNTIL)
    await clear_persistent_mute(player)
    await player.arefresh_from_db()
    assert player.muted_until is None


# --- reapply_mute_on_login ---


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_reapply_permanent_mute_sends_true():
    from amc.factories import PlayerFactory

    player = await sync_to_async(PlayerFactory)(muted_until=PERMANENT_MUTE_UNTIL)
    session = MagicMock()
    # mute.py imports mute_player inside the function body, so patch the SOURCE
    # module (amc.mod_server.mute_player), not amc.mute.mute_player.
    with patch("amc.mod_server.mute_player", new_callable=AsyncMock) as mock_mute:
        await reapply_mute_on_login(player, session)
    mock_mute.assert_awaited_once_with(
        session, player.unique_id, mute_for=True, hard=True
    )
    # sentinel is not cleared
    await player.arefresh_from_db()
    assert player.muted_until is not None


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_reapply_temp_mute_sends_remaining_seconds():
    from amc.factories import PlayerFactory

    until = timezone.now() + timedelta(minutes=5)
    player = await sync_to_async(PlayerFactory)(muted_until=until)
    session = MagicMock()
    with patch("amc.mod_server.mute_player", new_callable=AsyncMock) as mock_mute:
        await reapply_mute_on_login(player, session)
    mock_mute.assert_awaited_once()
    kwargs = mock_mute.await_args.kwargs
    assert isinstance(kwargs["mute_for"], int)
    assert 200 < kwargs["mute_for"] < 400  # ~5 min remaining
    assert kwargs["hard"] is True


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_reapply_no_mute_is_noop():
    from amc.factories import PlayerFactory

    player = await sync_to_async(PlayerFactory)()
    session = MagicMock()
    with patch("amc.mod_server.mute_player", new_callable=AsyncMock) as mock_mute:
        await reapply_mute_on_login(player, session)
    mock_mute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_reapply_expired_temp_clears_and_does_not_mute():
    from amc.factories import PlayerFactory

    player = await sync_to_async(PlayerFactory)(
        muted_until=timezone.now() - timedelta(minutes=1)
    )
    session = MagicMock()
    with patch("amc.mod_server.mute_player", new_callable=AsyncMock) as mock_mute:
        await reapply_mute_on_login(player, session)
    mock_mute.assert_not_awaited()
    await player.arefresh_from_db()
    assert player.muted_until is None


# --- admin command persistence ---


async def _make_ctx(player, character, *, is_admin=False, mod_session=None):
    from amc.command_framework import CommandContext

    ctx = CommandContext(
        timestamp=None,
        character=character,
        player=player,
        http_client=MagicMock(),
        http_client_mod=mod_session or MagicMock(),
        player_info=({"bIsAdmin": True} if is_admin else {"bIsAdmin": False}),
    )
    ctx.reply = AsyncMock()
    ctx.announce = AsyncMock()
    return ctx


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_cmd_mute_persists_permanent():
    from amc.commands.admin import cmd_mute
    from amc.factories import PlayerFactory, CharacterFactory

    admin = await sync_to_async(PlayerFactory)()
    admin_char = await sync_to_async(CharacterFactory)(
        player=admin, name="Admin", guid="guid-admin-mute"
    )
    target = await sync_to_async(PlayerFactory)()
    await sync_to_async(CharacterFactory)(
        player=target, name="Troublemaker", guid="guid-target-mute"
    )

    with (
        patch(
            "amc.commands.admin.get_players_mod",
            new_callable=AsyncMock,
            return_value=[
                {
                    "PlayerName": "Troublemaker",
                    "CharacterGuid": "guid-target-mute",
                    "UniqueID": target.unique_id,
                }
            ],
        ),
        patch("amc.commands.admin.mute_player", new_callable=AsyncMock),
    ):
        ctx = await _make_ctx(admin, admin_char, is_admin=True)
        await cmd_mute(ctx, "Troublemaker")  # no duration → permanent

    await target.arefresh_from_db()
    assert target.muted_until is not None
    assert target.muted_until.year >= 9999


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_cmd_unmute_clears_persistence():
    from amc.commands.admin import cmd_unmute
    from amc.factories import PlayerFactory, CharacterFactory

    admin = await sync_to_async(PlayerFactory)()
    admin_char = await sync_to_async(CharacterFactory)(
        player=admin, name="Admin", guid="guid-admin-unmute"
    )
    target = await sync_to_async(PlayerFactory)(muted_until=PERMANENT_MUTE_UNTIL)
    await sync_to_async(CharacterFactory)(
        player=target, name="MutedGuy", guid="guid-target-unmute"
    )

    with (
        patch(
            "amc.commands.admin.get_players_mod",
            new_callable=AsyncMock,
            return_value=[
                {
                    "PlayerName": "MutedGuy",
                    "CharacterGuid": "guid-target-unmute",
                    "UniqueID": target.unique_id,
                }
            ],
        ),
        patch("amc.commands.admin.unmute_player", new_callable=AsyncMock),
    ):
        ctx = await _make_ctx(admin, admin_char, is_admin=True)
        await cmd_unmute(ctx, "MutedGuy")

    await target.arefresh_from_db()
    assert target.muted_until is None
