from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async

from amc.command_framework import CommandContext
from amc.commands.admin import cmd_spawn_dealership
from amc.factories import CharacterFactory, PlayerFactory
from amc.models import VehicleDealership

PLAYER_LOC = {"X": -286981.0, "Y": 188839.0, "Z": -21812.0}


def make_ctx(player, character, player_info):
    return CommandContext(
        timestamp=None,
        character=character,
        player=player,
        http_client=MagicMock(),
        http_client_mod=MagicMock(),
        player_info=player_info,
    )


async def _make_pair():
    player = await sync_to_async(PlayerFactory)(characters=[])
    character = await sync_to_async(CharacterFactory)(player=player)
    return player, character


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_non_admin_is_noop():
    player, character = await _make_pair()
    ctx = make_ctx(player, character, {"bIsAdmin": False, "Location": dict(PLAYER_LOC)})

    with patch(
        "amc.commands.admin.teleport_player", new_callable=AsyncMock
    ) as m_tp, patch(
        "amc.commands.admin.spawn_dealership", new_callable=AsyncMock
    ) as m_sd, patch("amc.mod_server.show_popup", new_callable=AsyncMock):
        await cmd_spawn_dealership(ctx, "Kart")

    m_tp.assert_not_awaited()
    m_sd.assert_not_awaited()
    assert not await VehicleDealership.objects.filter(
        notes=f"/spawn_dealership by {character.name}"
    ).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_no_args_lists_vehicle_labels():
    player, character = await _make_pair()
    ctx = make_ctx(player, character, {"bIsAdmin": True, "Location": dict(PLAYER_LOC)})

    with patch(
        "amc.commands.admin.teleport_player", new_callable=AsyncMock
    ) as m_tp, patch(
        "amc.commands.admin.spawn_dealership", new_callable=AsyncMock
    ) as m_sd, patch(
        "amc.mod_server.show_popup", new_callable=AsyncMock
    ) as m_popup:
        await cmd_spawn_dealership(ctx)

    m_tp.assert_not_awaited()
    m_sd.assert_not_awaited()
    # the listing popup must include the Kart label
    assert m_popup.await_count == 1
    assert "Kart" in m_popup.await_args.args[1]
    assert not await VehicleDealership.objects.filter(
        notes=f"/spawn_dealership by {character.name}"
    ).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_unknown_label_aborts_without_spawning():
    player, character = await _make_pair()
    ctx = make_ctx(player, character, {"bIsAdmin": True, "Location": dict(PLAYER_LOC)})

    with patch(
        "amc.commands.admin.teleport_player", new_callable=AsyncMock
    ) as m_tp, patch(
        "amc.commands.admin.spawn_dealership", new_callable=AsyncMock
    ) as m_sd, patch("amc.mod_server.show_popup", new_callable=AsyncMock):
        await cmd_spawn_dealership(ctx, "NotACar")

    m_tp.assert_not_awaited()
    m_sd.assert_not_awaited()
    assert not await VehicleDealership.objects.filter(
        notes=f"/spawn_dealership by {character.name}"
    ).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_teleports_up_then_spawns_pad_at_z_minus_100():
    player, character = await _make_pair()
    ctx = make_ctx(player, character, {"bIsAdmin": True, "Location": dict(PLAYER_LOC)})

    with patch(
        "amc.commands.admin.teleport_player", new_callable=AsyncMock
    ) as m_tp, patch(
        "amc.commands.admin.spawn_dealership", new_callable=AsyncMock
    ) as m_sd, patch("amc.mod_server.show_popup", new_callable=AsyncMock):
        order = []
        m_tp.side_effect = lambda *a, **k: order.append("tp")
        m_sd.side_effect = lambda *a, **k: order.append("sd")
        await cmd_spawn_dealership(ctx, "Kart")

    # teleport FIRST (player away from the pad origin), spawn IMMEDIATELY
    # after — no sleep, gravity brings the player back down (freeman rule)
    assert order == ["tp", "sd"]

    # teleport UP: z + 300, same X/Y, steam unique_id as player_id
    m_tp.assert_awaited_once_with(
        ctx.http_client_mod,
        str(player.unique_id),
        {"X": -286981.0, "Y": 188839.0, "Z": -21512.0},  # -21812 + 300
    )
    # pad at playerZ - 100
    m_sd.assert_awaited_once_with(
        ctx.http_client_mod,
        "Kart_01",  # "Kart" label -> VehicleKey value
        {"X": -286981.0, "Y": 188839.0, "Z": -21912.0},  # -21812 - 100
        0.0,
    )


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_creates_persistent_dealership_row():
    player, character = await _make_pair()
    ctx = make_ctx(player, character, {"bIsAdmin": True, "Location": dict(PLAYER_LOC)})

    with patch(
        "amc.commands.admin.teleport_player", new_callable=AsyncMock
    ), patch(
        "amc.commands.admin.spawn_dealership", new_callable=AsyncMock
    ), patch("amc.mod_server.show_popup", new_callable=AsyncMock):
        await cmd_spawn_dealership(ctx, "Kart")

    rows = [
        r
        async for r in VehicleDealership.objects.filter(
            notes=f"/spawn_dealership by {character.name}"
        )
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row is not None
    assert row.vehicle_key == "Kart_01"
    assert row.location.x == pytest.approx(-286981.0)
    assert row.location.y == pytest.approx(188839.0)
    assert row.location.z == pytest.approx(-21912.0)
    assert row.yaw == 0.0
    assert row.spawn_on_restart is True


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_teleport_failure_aborts_before_spawn():
    player, character = await _make_pair()
    ctx = make_ctx(player, character, {"bIsAdmin": True, "Location": dict(PLAYER_LOC)})

    with patch(
        "amc.commands.admin.teleport_player", new_callable=AsyncMock
    ) as m_tp, patch(
        "amc.commands.admin.spawn_dealership", new_callable=AsyncMock
    ) as m_sd, patch("amc.mod_server.show_popup", new_callable=AsyncMock):
        m_tp.side_effect = Exception("boom")
        await cmd_spawn_dealership(ctx, "Kart")

    m_sd.assert_not_awaited()
    assert not await VehicleDealership.objects.filter(
        notes=f"/spawn_dealership by {character.name}"
    ).aexists()
