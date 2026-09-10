from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async

from amc.command_framework import CommandContext
from amc.commands.admin import cmd_spawn_object
from amc.factories import CharacterFactory, PlayerFactory
from amc.models import Player, WorldObject
from amc.spawnable_objects import (
    SPAWNABLE_OBJECT_CATEGORIES,
    SPAWNABLE_OBJECTS,
)

PLAYER_LOC = {"X": -286981.0, "Y": 188839.0, "Z": -21812.0}
VIEW_LOC = {"X": -280000.0, "Y": 190000.0, "Z": -20000.0}

CRANE_PATH = SPAWNABLE_OBJECTS["engine_crane"].asset_path


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


async def _cleanup(player):
    # async acreate rows leak past pytest-django transactions — remove our own
    await Player.objects.filter(unique_id=player.unique_id).adelete()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_non_admin_is_noop():
    player, character = await _make_pair()
    try:
        ctx = make_ctx(player, character, {"bIsAdmin": False, "Location": dict(PLAYER_LOC)})

        with patch(
            "amc.commands.admin.spawn_assets", new_callable=AsyncMock
        ) as m_spawn, patch(
            "amc.commands.admin.get_player", new_callable=AsyncMock
        ) as m_gp, patch("amc.mod_server.show_popup", new_callable=AsyncMock):
            await cmd_spawn_object(ctx, "engine_crane")

        m_spawn.assert_not_awaited()
        m_gp.assert_not_awaited()
        assert not await WorldObject.objects.filter(asset_path=CRANE_PATH).aexists()
    finally:
        await _cleanup(player)


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_no_args_lists_aliases():
    player, character = await _make_pair()
    try:
        ctx = make_ctx(player, character, {"bIsAdmin": True, "Location": dict(PLAYER_LOC)})

        with patch(
            "amc.commands.admin.spawn_assets", new_callable=AsyncMock
        ) as m_spawn, patch(
            "amc.mod_server.show_popup", new_callable=AsyncMock
        ) as m_popup:
            await cmd_spawn_object(ctx)

        m_spawn.assert_not_awaited()
        assert m_popup.await_count == 1
        message = m_popup.await_args.args[1]
        assert "Spawnable Objects" in message
        assert "engine_crane" in message
        assert "Garage equipment" in message
        assert not await WorldObject.objects.filter(asset_path=CRANE_PATH).aexists()
    finally:
        await _cleanup(player)


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_unknown_alias_aborts_without_spawning():
    player, character = await _make_pair()
    try:
        ctx = make_ctx(player, character, {"bIsAdmin": True, "Location": dict(PLAYER_LOC)})

        with patch(
            "amc.commands.admin.spawn_assets", new_callable=AsyncMock
        ) as m_spawn, patch(
            "amc.mod_server.show_popup", new_callable=AsyncMock
        ) as m_popup:
            await cmd_spawn_object(ctx, "not_a_thing")

        m_spawn.assert_not_awaited()
        message = m_popup.await_args.args[1]
        assert "Unknown Alias" in message
        assert "not_a_thing" in message
        assert not await WorldObject.objects.filter(asset_path=CRANE_PATH).aexists()
    finally:
        await _cleanup(player)


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_spawns_at_player_location_and_persists():
    player, character = await _make_pair()
    try:
        ctx = make_ctx(player, character, {"bIsAdmin": True, "Location": dict(PLAYER_LOC)})

        with patch(
            "amc.commands.admin.spawn_assets", new_callable=AsyncMock
        ) as m_spawn, patch(
            "amc.commands.admin.get_player", new_callable=AsyncMock
        ) as m_gp, patch(
            "amc.mod_server.show_popup", new_callable=AsyncMock
        ) as m_popup:
            m_gp.return_value = None  # no pawn data -> Location - 30, yaw 0.0
            await cmd_spawn_object(ctx, "engine_crane")

        expected_loc = {
            "X": -286981.0,
            "Y": 188839.0,
            "Z": -21842.0,  # -21812 - 30
        }
        m_spawn.assert_awaited_once_with(
            ctx.http_client_mod,
            [{"AssetPath": CRANE_PATH, "Location": expected_loc, "Rotation": {}}],
        )

        rows = [
            r async for r in WorldObject.objects.filter(asset_path=CRANE_PATH)
        ]
        assert len(rows) == 1
        row = rows[0]
        assert row.location_x == pytest.approx(-286981.0)
        assert row.location_y == pytest.approx(188839.0)
        assert row.location_z == pytest.approx(-21842.0)
        assert row.yaw == 0.0
        assert row.scale == 1.0

        message = m_popup.await_args.args[1]
        assert "Object Spawned" in message
        assert "engine_crane" in message
    finally:
        await WorldObject.objects.filter(asset_path=CRANE_PATH).adelete()
        await _cleanup(player)


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_uses_view_location_when_available():
    player, character = await _make_pair()
    try:
        ctx = make_ctx(player, character, {"bIsAdmin": True, "Location": dict(PLAYER_LOC)})

        with patch(
            "amc.commands.admin.spawn_assets", new_callable=AsyncMock
        ) as m_spawn, patch(
            "amc.commands.admin.get_player", new_callable=AsyncMock
        ) as m_gp, patch("amc.mod_server.show_popup", new_callable=AsyncMock):
            m_gp.return_value = {"ViewLocation": dict(VIEW_LOC)}
            await cmd_spawn_object(ctx, "engine_crane")

        # ViewLocation is used verbatim (no Z offset)
        m_spawn.assert_awaited_once_with(
            ctx.http_client_mod,
            [{"AssetPath": CRANE_PATH, "Location": dict(VIEW_LOC), "Rotation": {}}],
        )
        row = await WorldObject.objects.filter(asset_path=CRANE_PATH).afirst()
        assert row is not None
        assert row.location_z == pytest.approx(VIEW_LOC["Z"])
    finally:
        await WorldObject.objects.filter(asset_path=CRANE_PATH).adelete()
        await _cleanup(player)


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_uses_pawn_yaw_for_spawn_and_row():
    player, character = await _make_pair()
    try:
        ctx = make_ctx(player, character, {"bIsAdmin": True, "Location": dict(PLAYER_LOC)})

        with patch(
            "amc.commands.admin.spawn_assets", new_callable=AsyncMock
        ) as m_spawn, patch(
            "amc.commands.admin.get_player", new_callable=AsyncMock
        ) as m_gp, patch("amc.mod_server.show_popup", new_callable=AsyncMock):
            m_gp.return_value = {"Rotation": {"Roll": 0.0, "Pitch": 0.0, "Yaw": 170.0}}
            await cmd_spawn_object(ctx, "engine_crane")

        m_spawn.assert_awaited_once_with(
            ctx.http_client_mod,
            [
                {
                    "AssetPath": CRANE_PATH,
                    "Location": {
                        "X": -286981.0,
                        "Y": 188839.0,
                        "Z": -21842.0,
                    },
                    "Rotation": {"Roll": 0.0, "Pitch": 0.0, "Yaw": 170.0},
                }
            ],
        )
        row = await WorldObject.objects.filter(asset_path=CRANE_PATH).afirst()
        assert row is not None
        assert row.yaw == pytest.approx(170.0)
    finally:
        await WorldObject.objects.filter(asset_path=CRANE_PATH).adelete()
        await _cleanup(player)


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_get_player_failure_falls_back_to_player_location():
    player, character = await _make_pair()
    try:
        ctx = make_ctx(player, character, {"bIsAdmin": True, "Location": dict(PLAYER_LOC)})

        with patch(
            "amc.commands.admin.spawn_assets", new_callable=AsyncMock
        ) as m_spawn, patch(
            "amc.commands.admin.get_player", new_callable=AsyncMock
        ) as m_gp, patch("amc.mod_server.show_popup", new_callable=AsyncMock):
            m_gp.side_effect = Exception("mod endpoint down")
            await cmd_spawn_object(ctx, "engine_crane")

        # a failed yaw fetch must not abort the spawn
        m_spawn.assert_awaited_once_with(
            ctx.http_client_mod,
            [
                {
                    "AssetPath": CRANE_PATH,
                    "Location": {
                        "X": -286981.0,
                        "Y": 188839.0,
                        "Z": -21842.0,
                    },
                    "Rotation": {},
                }
            ],
        )
        row = await WorldObject.objects.filter(asset_path=CRANE_PATH).afirst()
        assert row is not None
        assert row.yaw == 0.0
    finally:
        await WorldObject.objects.filter(asset_path=CRANE_PATH).adelete()
        await _cleanup(player)


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_alias_is_case_insensitive():
    player, character = await _make_pair()
    try:
        ctx = make_ctx(player, character, {"bIsAdmin": True, "Location": dict(PLAYER_LOC)})

        with patch(
            "amc.commands.admin.spawn_assets", new_callable=AsyncMock
        ) as m_spawn, patch(
            "amc.commands.admin.get_player", new_callable=AsyncMock
        ) as m_gp, patch("amc.mod_server.show_popup", new_callable=AsyncMock):
            m_gp.return_value = None
            await cmd_spawn_object(ctx, "  ENGINE_CRANE  ")

        m_spawn.assert_awaited_once()
        payload = m_spawn.await_args.args[1]
        assert payload[0]["AssetPath"] == CRANE_PATH
    finally:
        await WorldObject.objects.filter(asset_path=CRANE_PATH).adelete()
        await _cleanup(player)


def test_spawnable_objects_data_integrity():
    # aliases are unique + normalised
    aliases = list(SPAWNABLE_OBJECTS.keys())
    assert len(aliases) == len(set(aliases))
    assert all(a == a.strip().lower() for a in aliases)

    for obj in SPAWNABLE_OBJECTS.values():
        assert obj.asset_path.startswith("/Game/"), obj
        assert obj.description
        assert obj.category in SPAWNABLE_OBJECT_CATEGORIES

    # every category bucket is non-empty and ordered first-to-last
    seen = {obj.category for obj in SPAWNABLE_OBJECTS.values()}
    assert seen == set(SPAWNABLE_OBJECT_CATEGORIES)


def test_mesh_paths_use_package_slash_form():
    # Regression (prod 2026-09-10): mesh paths were built as
    # "/Game/.../Props.SM_Prop_X.SM_Prop_X" — a dot before the package's mesh
    # name addresses a package that does not exist, so the mod's LoadAsset +
    # re-find fails and every mesh alias 500'd with "Failed to spawn asset".
    # Each workshop mesh is its own cooked package, so the package part of
    # the path must end with "/<MeshName>"; blueprint classes keep the
    # "<Pkg>.<Pkg>_C" form.
    for obj in SPAWNABLE_OBJECTS.values():
        pkg, sep, obj_name = obj.asset_path.rpartition(".")
        assert sep and pkg and obj_name and "." not in obj_name, obj
        if obj_name.endswith("_C"):
            assert pkg.rsplit("/", 1)[-1] + "_C" == obj_name, obj
        else:
            assert pkg.endswith("/" + obj_name), obj
