"""PoliceWhitelist: /police gate, whitelist totals, admin toggle."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async

from amc.factories import CharacterFactory, PlayerFactory
from amc.models import PoliceSession, PoliceWhitelist


async def _make_officer(name: str, guid: str, total: int = 0):
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player,
        name=name,
        guid=guid,
    )
    if total:
        await PoliceWhitelist.objects.acreate(
            player=player, police_confiscated_total=total
        )
    return player, character


# --- record_confiscation_for_level ---


@pytest.mark.django_db
@pytest.mark.asyncio
@patch("amc.police.refresh_player_name", new_callable=AsyncMock)
async def test_confiscation_without_whitelist_no_resurrection(mock_refresh):
    """Confiscating for an un-whitelisted player does NOT create a row —
    removing an officer mid-duty must not silently re-whitelist them."""
    from amc.police import record_confiscation_for_level

    player, character = await _make_officer("OfficerOne", "guid-wl-upsert")

    await record_confiscation_for_level(character, 20_000, session=MagicMock())

    assert not await PoliceWhitelist.objects.filter(player=player).aexists()


@pytest.mark.django_db
@pytest.mark.asyncio
@patch("amc.police.refresh_player_name", new_callable=AsyncMock)
async def test_confiscation_total_persists_across_characters(mock_refresh):
    """Level totals are per player — a new character reads the same row."""
    from amc.police import record_confiscation_for_level

    player, _first = await _make_officer(
        "OfficerOne", "guid-wl-cross", total=49_999
    )
    second = await sync_to_async(CharacterFactory)(
        player=player, name="OfficerOneAlt", guid="guid-wl-cross-alt"
    )

    await record_confiscation_for_level(second, 1, session=MagicMock())

    row = await PoliceWhitelist.objects.aget(player=player)
    assert row.police_confiscated_total == 50_000


@pytest.mark.django_db
@pytest.mark.asyncio
@patch("amc.police.refresh_player_name", new_callable=AsyncMock)
async def test_confiscation_updates_existing_whitelist_row(mock_refresh):
    """Whitelisted officer: confiscation lands on the PLAYER's row."""
    from amc.police import record_confiscation_for_level

    player, character = await _make_officer(
        "OfficerOne", "guid-wl-upsert2", total=10_000
    )

    await record_confiscation_for_level(character, 20_000, session=MagicMock())

    row = await PoliceWhitelist.objects.aget(player=player)
    assert row.police_confiscated_total == 30_000


@pytest.mark.django_db
@pytest.mark.asyncio
@patch("amc.police.refresh_player_name", new_callable=AsyncMock)
async def test_confiscation_level_up_refreshes_name(mock_refresh):
    """Crossing POLICE_LEVEL_STEP triggers a name refresh (level-up path)."""
    from amc.police import record_confiscation_for_level

    player, character = await _make_officer(
        "OfficerOne", "guid-wl-levelup", total=49_999
    )

    await record_confiscation_for_level(
        character, 1, http_client=MagicMock(), session=MagicMock()
    )

    mock_refresh.assert_awaited_once()
    row = await PoliceWhitelist.objects.aget(player=player)
    assert row.police_confiscated_total == 50_000


# --- /police whitelist gate (dispatch through the registry) ---


def _ctx_for(player, character, admin=False):
    from amc.command_framework import CommandContext

    ctx = MagicMock(spec=CommandContext)
    ctx.player = player
    ctx.character = character
    ctx.reply = AsyncMock()
    ctx.announce = AsyncMock()
    ctx.http_client = MagicMock()
    ctx.http_client_mod = MagicMock()
    ctx.player_info = (
        {"bIsAdmin": True, "unique_id": str(player.unique_id)} if admin else {}
    )
    return ctx


@pytest.mark.django_db
@pytest.mark.asyncio
@patch("amc.commands.police.send_system_message", new_callable=AsyncMock)
async def test_police_requires_whitelist(mock_msg):
    """A non-whitelisted player cannot go on duty."""
    from amc.command_framework import registry
    from amc.commands.police import cmd_police  # ensure module registered

    assert cmd_police is not None
    player, character = await _make_officer("Civilian", "guid-wl-civ")
    ctx = _ctx_for(player, character)

    await registry.execute("/police", ctx)

    mock_msg.assert_awaited_once()
    assert not await PoliceSession.objects.filter(character=character).aexists()


@pytest.mark.django_db
@pytest.mark.asyncio
@patch("amc.commands.police.send_system_message", new_callable=AsyncMock)
async def test_police_whitelisted_passes_gate(mock_msg):
    """A whitelisted player gets past the gate (fails later on costume)."""
    from amc.command_framework import registry

    player, character = await _make_officer("Rookie", "guid-wl-rookie")
    await PoliceWhitelist.objects.acreate(player=player)
    ctx = _ctx_for(player, character)

    with patch(
        "amc.commands.police.get_player_customization", new_callable=AsyncMock
    ) as mock_cust, patch(
        "amc.commands.police.show_popup", new_callable=AsyncMock
    ):
        mock_cust.return_value = {"Costume": "Costume_Civil_01"}
        await registry.execute("/police", ctx)

    # Gate passed — the costume popup fired instead of the roster message.
    assert mock_msg.await_count == 0
    assert not await PoliceSession.objects.filter(character=character).aexists()


# --- /police_whitelist admin toggle ---


@pytest.mark.django_db
@pytest.mark.asyncio
@patch("amc.commands.police.get_players")
async def test_police_whitelist_add_and_remove(mock_players):
    from amc.command_framework import registry

    admin_player, _ = await _make_officer("TheAdmin", "guid-wl-admin")
    target_player, target_character = await _make_officer(
        "NewCop", "guid-wl-newcop"
    )

    ctx = _ctx_for(admin_player, target_character, admin=True)
    ctx.player.unique_id = admin_player.unique_id

    mock_players.return_value = [
        (admin_player.unique_id, {"name": "TheAdmin"}),
        (
            target_player.unique_id,
            {
                "name": "NewCop",
                "character_guid": target_character.guid,
                "location": "X=0 Y=0 Z=0",
            },
        ),
    ]

    # Off duty, so the toggle add is a no-op for sessions
    await registry.execute("/police_whitelist NewCop", ctx)
    assert await PoliceWhitelist.objects.filter(player=target_player).aexists()

    await registry.execute("/police_whitelist NewCop", ctx)
    assert not await PoliceWhitelist.objects.filter(
        player=target_player
    ).aexists()


@pytest.mark.django_db
@pytest.mark.asyncio
@patch("amc.commands.police.get_players")
async def test_police_whitelist_non_admin_silent(mock_players):
    from amc.command_framework import registry

    caller, caller_character = await _make_officer("Pleb", "guid-wl-pleb")
    ctx = _ctx_for(caller, caller_character, admin=False)
    ctx.player.unique_id = caller.unique_id

    mock_players.return_value = []

    await registry.execute("/police_whitelist Anyone", ctx)

    mock_players.assert_not_called()
    assert not await PoliceWhitelist.objects.filter(player=caller).aexists()


@pytest.mark.django_db
@pytest.mark.asyncio
@patch("amc.no_teleport.sync_no_teleport", new_callable=AsyncMock)
@patch("amc.police.refresh_player_name", new_callable=AsyncMock)
@patch("amc.commands.police.get_players")
async def test_police_whitelist_remove_ends_active_session(
    mock_players, mock_refresh, mock_sync
):
    """Removing a whitelisted officer deactivates their active session."""
    from amc.command_framework import registry

    admin_player, admin_character = await _make_officer("TheAdmin", "guid-wl-adm2")
    target_player, target_character = await _make_officer(
        "RogueCop", "guid-wl-rogue"
    )
    await PoliceWhitelist.objects.acreate(player=target_player)
    await PoliceSession.objects.acreate(character=target_character)

    ctx = _ctx_for(admin_player, admin_character, admin=True)
    ctx.player.unique_id = admin_player.unique_id

    mock_players.return_value = [
        (
            target_player.unique_id,
            {
                "name": "RogueCop",
                "character_guid": target_character.guid,
                "location": "X=0 Y=0 Z=0",
            },
        ),
    ]

    await registry.execute("/police_whitelist RogueCop", ctx)

    session = await PoliceSession.objects.aget(character=target_character)
    assert session.ended_at is not None
