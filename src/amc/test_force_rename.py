import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from asgiref.sync import sync_to_async

from amc.player_tags import refresh_player_name
from amc.commands.admin import (
    cmd_force_rename,
    cmd_clear_forced_name,
    _validate_forced_name,
    _resolve_player_for_force_rename,
    _resolve_offline_player_by_name,
)
from amc.commands.general import cmd_rename


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


# --- refresh_player_name forced-name behaviour ---


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_uses_forced_name(mock_set_name):
    from amc.factories import CharacterFactory, PlayerFactory

    player = await sync_to_async(PlayerFactory)(forced_name="NewForced")
    character = await sync_to_async(CharacterFactory)(
        player=player, name="OffensiveName", guid="guid-force-1"
    )

    session = MagicMock()
    await refresh_player_name(character, session, has_custom_parts=False)

    await character.arefresh_from_db()
    assert character.custom_name == "NewForced"
    from amc.player_tags import set_character_name

    set_character_name.assert_awaited_once_with(session, "guid-force-1", "NewForced")


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_forced_name_still_gets_tags(mock_set_name):
    """A forced name still receives the standard MOD tag when custom parts are on."""
    from amc.factories import CharacterFactory, PlayerFactory

    player = await sync_to_async(PlayerFactory)(forced_name="NewForced")
    character = await sync_to_async(CharacterFactory)(
        player=player, name="OffensiveName", guid="guid-force-2"
    )

    session = MagicMock()
    await refresh_player_name(character, session, has_custom_parts=True)

    await character.arefresh_from_db()
    assert character.custom_name == "[M] NewForced"


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_forced_name_survives_character_switch(mock_set_name):
    """A forced name applies to ANY character of the player (account-level)."""
    from amc.factories import CharacterFactory, PlayerFactory

    player = await sync_to_async(PlayerFactory)(forced_name="LockedName")
    char2 = await sync_to_async(CharacterFactory)(
        player=player, name="SecondChar", guid="guid-force-3"
    )

    session = MagicMock()
    await refresh_player_name(char2, session, has_custom_parts=False)

    await char2.arefresh_from_db()
    assert char2.custom_name == "LockedName"


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_restores_chosen_name_after_clear(mock_set_name):
    from amc.factories import CharacterFactory, PlayerFactory

    player = await sync_to_async(PlayerFactory)(forced_name="LockedName")
    character = await sync_to_async(CharacterFactory)(
        player=player, name="ChosenName", guid="guid-force-4"
    )

    session = MagicMock()
    await refresh_player_name(character, session, has_custom_parts=False)
    await character.arefresh_from_db()
    assert character.custom_name == "LockedName"

    # Admin clears the lock
    player.forced_name = None
    await player.asave(update_fields=["forced_name"])
    await refresh_player_name(character, session, has_custom_parts=False)

    await character.arefresh_from_db()
    assert character.custom_name == "ChosenName"
    assert "ChosenName" in mock_set_name.call_args.args[2]


# --- /rename is blocked under a forced name ---


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_cmd_rename_blocked_when_forced_name_set():
    from amc.factories import CharacterFactory, PlayerFactory

    player = await sync_to_async(PlayerFactory)(forced_name="LockedName")
    character = await sync_to_async(CharacterFactory)(
        player=player, name="OldName", guid="guid-rename-1"
    )

    ctx = await _make_ctx(player, character)
    await cmd_rename(ctx, "BrandNewName")

    await character.arefresh_from_db()
    assert character.name == "OldName"  # unchanged
    ctx.reply.assert_awaited_once()
    assert "locked" in ctx.reply.await_args.args[0].lower()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_cmd_rename_allowed_when_no_forced_name():
    """Without a forced name, /rename still works normally."""
    from amc.factories import CharacterFactory, PlayerFactory
    from asgiref.sync import sync_to_async as s2a

    player = await s2a(PlayerFactory)()
    character = await s2a(CharacterFactory)(player=player, name="OldName", guid="g-r-2")

    with patch(
        "amc.commands.general.refresh_player_name", new_callable=AsyncMock
    ) as mock_refresh:
        ctx = await _make_ctx(player, character)
        await cmd_rename(ctx, "NewOkName")

    await character.arefresh_from_db()
    assert character.name == "NewOkName"
    mock_refresh.assert_awaited_once()


# --- admin commands ---


def test_validate_forced_name():
    assert _validate_forced_name("CleanName") == "CleanName"
    assert _validate_forced_name("  [M] HasTag  ") == "HasTag"
    assert _validate_forced_name("x" * 21) is None  # too long
    assert _validate_forced_name("Bad(Name") is None  # contains "("
    # Empty / whitespace / tag-only results must be rejected, otherwise the
    # saved lock would be a falsy '' that blocks neither /rename nor reports.
    assert _validate_forced_name("") is None
    assert _validate_forced_name("   ") is None
    assert _validate_forced_name("[M]") is None
    assert _validate_forced_name("[GOV1]") is None


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_resolve_player_offline_fallback():
    """A player not online can still be resolved from the DB by stored name."""
    from amc.factories import CharacterFactory, PlayerFactory

    target_player = await sync_to_async(PlayerFactory)()
    await sync_to_async(CharacterFactory)(
        player=target_player, name="OfflineGuy", guid="guid-offline-1"
    )

    # No online players reported by the mod server.
    with patch(
        "amc.commands.admin.get_players_mod",
        new_callable=AsyncMock,
        return_value=None,
    ):
        player, character = await _resolve_player_for_force_rename(
            MagicMock(), "OfflineGuy"
        )

    assert player is not None
    assert player.pk == target_player.pk
    # Offline → no online character to push to
    assert character is None

    # Also resolve straight through the offline helper ignoring online list.
    player2, _char2 = await _resolve_offline_player_by_name("OfflineGuy")
    assert player2 is not None and player2.pk == target_player.pk


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_resolve_player_online_takes_precedence():
    """The online player's character (with GUID) is preferred over DB fallback."""
    from amc.factories import CharacterFactory, PlayerFactory

    target_player = await sync_to_async(PlayerFactory)()
    await sync_to_async(CharacterFactory)(
        player=target_player, name="OnlineGuy", guid="guid-online-1"
    )

    mod_session = MagicMock()
    with patch(
        "amc.commands.admin.get_players_mod",
        new_callable=AsyncMock,
    ) as mock_players:
        mock_players.return_value = [
            {"PlayerName": "OnlineGuy", "CharacterGuid": "guid-online-1"}
        ]
        player, character = await _resolve_player_for_force_rename(
            mod_session, "OnlineGuy"
        )

    assert player is not None and player.pk == target_player.pk
    assert character is not None and character.guid == "guid-online-1"


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_cmd_force_rename_sets_lock_and_applies():
    from amc.factories import CharacterFactory, PlayerFactory
    from amc.models import Character

    target_player = await sync_to_async(PlayerFactory)()
    target_character = await sync_to_async(CharacterFactory)(
        player=target_player, name="OffensiveName", guid="guid-target-1"
    )

    admin_player = await sync_to_async(PlayerFactory)()
    admin_character = await sync_to_async(CharacterFactory)(
        player=admin_player, name="Admin", guid="guid-admin-1"
    )

    mod_session = MagicMock()
    with (
        patch(
            "amc.commands.admin.get_players_mod",
            new_callable=AsyncMock,
        ) as mock_players,
        patch(
            "amc.commands.admin.refresh_player_name",
            new_callable=AsyncMock,
        ) as mock_refresh,
    ):
        mock_players.return_value = [
            {"PlayerName": "[M] OffensiveName", "CharacterGuid": "guid-target-1"}
        ]
        ctx = await _make_ctx(
            admin_player, admin_character, is_admin=True, mod_session=mod_session
        )
        await cmd_force_rename(ctx, "OffensiveName", "CleanedUp")

    await target_player.arefresh_from_db()
    assert target_player.forced_name == "CleanedUp"
    # Refresh must have re-applied using the now-loaded player relationship
    mock_refresh.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_cmd_force_rename_non_admin_denied():
    from amc.factories import CharacterFactory, PlayerFactory

    target_player = await sync_to_async(PlayerFactory)()
    target_character = await sync_to_async(CharacterFactory)(
        player=target_player, name="OffensiveName", guid="guid-target-2"
    )
    admin_player = await sync_to_async(PlayerFactory)()
    admin_character = await sync_to_async(CharacterFactory)(
        player=admin_player, name="Admin2", guid="guid-admin-2"
    )

    ctx = await _make_ctx(admin_player, admin_character, is_admin=False)
    await cmd_force_rename(ctx, "OffensiveName", "CleanedUp")

    await target_player.arefresh_from_db()
    assert target_player.forced_name is None  # not set


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_cmd_clear_forced_name_releases_lock():
    from amc.factories import CharacterFactory, PlayerFactory

    target_player = await sync_to_async(PlayerFactory)(forced_name="LockedName")
    target_character = await sync_to_async(CharacterFactory)(
        player=target_player, name="Original", guid="guid-target-3"
    )
    admin_player = await sync_to_async(PlayerFactory)()
    admin_character = await sync_to_async(CharacterFactory)(
        player=admin_player, name="Admin3", guid="guid-admin-3"
    )

    mod_session = MagicMock()
    with (
        patch(
            "amc.commands.admin.get_players_mod",
            new_callable=AsyncMock,
        ) as mock_players,
        patch(
            "amc.commands.admin.refresh_player_name",
            new_callable=AsyncMock,
        ) as mock_refresh,
    ):
        mock_players.return_value = [
            {"PlayerName": "LockedName", "CharacterGuid": "guid-target-3"}
        ]
        ctx = await _make_ctx(
            admin_player, admin_character, is_admin=True, mod_session=mod_session
        )
        await cmd_clear_forced_name(ctx, "LockedName")

    await target_player.arefresh_from_db()
    assert target_player.forced_name is None
    mock_refresh.assert_awaited()
