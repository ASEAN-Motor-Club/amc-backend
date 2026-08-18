from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async

from amc.commands.admin import (
    cmd_clear_forced_rp,
    cmd_force_rp,
)
from amc.forced_rp import apply_forced_rp, clear_forced_rp, is_forced_rp


@pytest.fixture(autouse=True)
def clear_cache():
    from django.core.cache import cache

    cache.clear()


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


# --- is_forced_rp ---


def test_is_forced_rp_none_when_unset():
    from amc.factories import PlayerFactory

    player = PlayerFactory.build()
    player.forced_rp_until = None
    assert is_forced_rp(player) is None


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_is_forced_rp_returns_until_when_future():
    from datetime import timedelta

    from django.utils import timezone

    player = await sync_to_async(_player_with_until)(timedelta(hours=2))
    until = is_forced_rp(player)
    assert until is not None
    assert until > timezone.now()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_is_forced_rp_none_after_expiry():
    from datetime import timedelta


    player = await sync_to_async(_player_with_until)(timedelta(hours=-2))
    assert is_forced_rp(player) is None


def _player_with_until(offset):
    from django.utils import timezone

    from amc.factories import PlayerFactory

    return PlayerFactory(forced_rp_until=timezone.now() + offset)


# --- apply_forced_rp / clear_forced_rp ---


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_apply_forced_rp_sets_lock_and_logs():
    from amc.factories import CharacterFactory, PlayerFactory
    from amc.models import ForcedRPLog

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player, name="Driver", guid="guid-rp-1"
    )
    actor_player = await sync_to_async(PlayerFactory)()
    actor_character = await sync_to_async(CharacterFactory)(
        player=actor_player, name="Admin", guid="guid-rp-admin"
    )

    with patch("amc.player_tags.refresh_player_name", new_callable=AsyncMock):
        duration = await apply_forced_rp(
            player,
            hours=2,
            http_client_mod=MagicMock(),
            actor_character=actor_character,
            actor_player=actor_player,
        )

    await player.arefresh_from_db()
    assert player.forced_rp_until is not None
    assert duration.total_seconds() == 2 * 3600

    # The online character is forced into RP mode.
    await character.arefresh_from_db()
    assert character.rp_mode is True

    log = await ForcedRPLog.objects.filter(player=player, action="set").afirst()
    assert log is not None
    assert log.actor_character_id == actor_character.pk
    assert log.actor_player_id == actor_player.pk


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_clear_forced_rp_releases_lock_and_logs():
    from datetime import timedelta

    from django.utils import timezone

    from amc.factories import PlayerFactory
    from amc.models import ForcedRPLog

    player = await sync_to_async(PlayerFactory)(
        forced_rp_until=timezone.now() + timedelta(hours=3)
    )

    cleared = await clear_forced_rp(player, actor_discord_id=12345)
    assert cleared is True

    await player.arefresh_from_db()
    assert player.forced_rp_until is None

    log = await ForcedRPLog.objects.filter(player=player, action="clear").afirst()
    assert log is not None
    assert log.actor_discord_id == 12345


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_clear_forced_rp_returns_false_when_no_lock():
    from amc.factories import PlayerFactory

    player = await sync_to_async(PlayerFactory)()
    assert await clear_forced_rp(player) is False


# --- /rp_mode toggle-off blocked while forced ---


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_cmd_rp_mode_toggle_off_blocked_while_forced():
    from datetime import timedelta

    from django.utils import timezone

    from amc.commands.rp_rescue import cmd_rp_mode
    from amc.factories import CharacterFactory, PlayerFactory

    player = await sync_to_async(PlayerFactory)(
        forced_rp_until=timezone.now() + timedelta(hours=5)
    )
    character = await sync_to_async(CharacterFactory)(
        player=player, name="Driver", guid="guid-block-1", rp_mode=True
    )

    ctx = MagicMock()
    ctx.character = character
    ctx.player = player
    ctx.http_client_mod = MagicMock()
    ctx.reply = AsyncMock()

    await cmd_rp_mode(ctx, verification_code="")

    # Locked — no toggle-off confirmation, just the locked message.
    ctx.reply.assert_awaited_once()
    text = str(ctx.reply.await_args.args[0])
    assert "Locked" in text or "locked" in text


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_cmd_rp_mode_toggle_off_allowed_after_lock_expires():
    from datetime import timedelta

    from django.utils import timezone

    from amc.commands.rp_rescue import cmd_rp_mode
    from amc.factories import CharacterFactory, PlayerFactory

    player = await sync_to_async(PlayerFactory)(
        forced_rp_until=timezone.now() - timedelta(hours=1)
    )
    character = await sync_to_async(CharacterFactory)(
        player=player, name="Driver", guid="guid-expire-1", rp_mode=True
    )

    ctx = MagicMock()
    ctx.character = character
    ctx.player = player
    ctx.http_client_mod = MagicMock()
    ctx.reply = AsyncMock()

    with patch("amc.commands.rp_rescue.refresh_player_name", new_callable=AsyncMock):
        await cmd_rp_mode(ctx, verification_code="")

    # Expired lock → normal toggle-off confirmation (not the locked message).
    ctx.reply.assert_awaited_once()
    text = str(ctx.reply.await_args.args[0])
    assert "Locked" not in text


# --- admin commands ---


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_cmd_force_rp_sets_lock_and_applies():
    from amc.factories import CharacterFactory, PlayerFactory

    target_player = await sync_to_async(PlayerFactory)()
    target_character = await sync_to_async(CharacterFactory)(
        player=target_player, name="Target", guid="guid-cmd-1"
    )
    admin_player = await sync_to_async(PlayerFactory)()
    admin_character = await sync_to_async(CharacterFactory)(
        player=admin_player, name="Admin", guid="guid-cmd-admin"
    )

    mod_session = MagicMock()
    with (
        patch(
            "amc.commands.admin.get_players_mod",
            new_callable=AsyncMock,
        ) as mock_players,
        patch("amc.player_tags.refresh_player_name", new_callable=AsyncMock),
    ):
        mock_players.return_value = [
            {"PlayerName": "Target", "CharacterGuid": "guid-cmd-1"}
        ]
        ctx = await _make_ctx(
            admin_player, admin_character, is_admin=True, mod_session=mod_session
        )
        await cmd_force_rp(ctx, "Target", "2")

    await target_player.arefresh_from_db()
    assert target_player.forced_rp_until is not None
    await target_character.arefresh_from_db()
    assert target_character.rp_mode is True


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_cmd_force_rp_non_admin_denied():
    from amc.factories import CharacterFactory, PlayerFactory

    target_player = await sync_to_async(PlayerFactory)()
    await sync_to_async(CharacterFactory)(
        player=target_player, name="Target", guid="guid-cmd-2"
    )
    admin_player = await sync_to_async(PlayerFactory)()
    admin_character = await sync_to_async(CharacterFactory)(
        player=admin_player, name="Admin", guid="guid-cmd-admin2"
    )

    ctx = await _make_ctx(admin_player, admin_character, is_admin=False)
    await cmd_force_rp(ctx, "Target", "2")

    await target_player.arefresh_from_db()
    assert target_player.forced_rp_until is None


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_cmd_force_rp_invalid_duration_rejected():
    from amc.factories import CharacterFactory, PlayerFactory

    target_player = await sync_to_async(PlayerFactory)()
    admin_player = await sync_to_async(PlayerFactory)()
    admin_character = await sync_to_async(CharacterFactory)(
        player=admin_player, name="Admin", guid="guid-cmd-admin3"
    )

    ctx = await _make_ctx(admin_player, admin_character, is_admin=True)
    await cmd_force_rp(ctx, "Target", "abc")
    await target_player.arefresh_from_db()
    assert target_player.forced_rp_until is None


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_cmd_clear_forced_rp_releases_lock():
    from datetime import timedelta

    from django.utils import timezone

    from amc.factories import CharacterFactory, PlayerFactory

    target_player = await sync_to_async(PlayerFactory)(
        forced_rp_until=timezone.now() + timedelta(hours=2)
    )
    await sync_to_async(CharacterFactory)(
        player=target_player, name="Target", guid="guid-cmd-3"
    )
    admin_player = await sync_to_async(PlayerFactory)()
    admin_character = await sync_to_async(CharacterFactory)(
        player=admin_player, name="Admin", guid="guid-cmd-admin4"
    )

    mod_session = MagicMock()
    with (
        patch(
            "amc.commands.admin.get_players_mod",
            new_callable=AsyncMock,
        ) as mock_players,
        patch("amc.player_tags.refresh_player_name", new_callable=AsyncMock),
    ):
        mock_players.return_value = [
            {"PlayerName": "Target", "CharacterGuid": "guid-cmd-3"}
        ]
        ctx = await _make_ctx(
            admin_player, admin_character, is_admin=True, mod_session=mod_session
        )
        await cmd_clear_forced_rp(ctx, "Target")

    await target_player.arefresh_from_db()
    assert target_player.forced_rp_until is None


# --- login enforcement ---


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_enforce_forced_rp_on_login_locks_character():
    from datetime import timedelta

    from django.utils import timezone

    from amc.factories import CharacterFactory, PlayerFactory
    from amc.forced_rp import enforce_forced_rp_on_login

    player = await sync_to_async(PlayerFactory)(
        forced_rp_until=timezone.now() + timedelta(hours=2)
    )
    character = await sync_to_async(CharacterFactory)(
        player=player, name="Driver", guid="guid-login-1", rp_mode=False
    )

    changed = await enforce_forced_rp_on_login(character, player)
    assert changed is True
    await character.arefresh_from_db()
    assert character.rp_mode is True


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_enforce_forced_rp_on_login_noop_when_not_locked():
    from amc.factories import CharacterFactory, PlayerFactory
    from amc.forced_rp import enforce_forced_rp_on_login

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player, name="Driver", guid="guid-login-2", rp_mode=False
    )

    changed = await enforce_forced_rp_on_login(character, player)
    assert changed is False
    await character.arefresh_from_db()
    assert character.rp_mode is False
