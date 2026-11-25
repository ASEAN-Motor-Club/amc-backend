"""Every vehicle-keeping /tp must use the same authoritative reset as /tp2marker.

If the backend omits bRemoveCargo, the mod falls back to its legacy
ClientResetVehicleAt RPC. The game rejects the resulting
ServerResetVehicleAtResponse as unsolicited, so the vehicle is never moved
authoritatively and despawns when the driver exits. Sending bRemoveCargo=True
selects the server-authoritative ServerResetVehicleAt branch and strips
cargo, matching /tp2marker.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from amc.command_framework import CommandContext
from amc.commands.teleport import cmd_tp_name
from amc.models import TeleportPoint


class _FakeQuerySet:
    """Minimal async queryset stand-in for the command's read paths."""

    def __init__(self, items=()):
        self.items = list(items)

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def select_related(self, *args, **kwargs):
        return self

    def __aiter__(self):
        async def gen():
            for item in self.items:
                yield item

        return gen()

    async def aexists(self):
        return bool(self.items)

    async def aget(self, *args, **kwargs):
        if not self.items:
            raise TeleportPoint.DoesNotExist
        return self.items[0]


def _make_ctx(is_admin=False):
    ctx = MagicMock(spec=CommandContext)
    ctx.reply = AsyncMock()
    ctx.announce = AsyncMock()
    ctx.http_client_mod = MagicMock()
    ctx.http_client_mod.get = AsyncMock()
    ctx.http_client_mod.post = AsyncMock()
    ctx.player_info = {
        "bIsAdmin": is_admin,
        "VehicleKey": "SomeVehicle",
        "CustomDestinationAbsoluteLocation": {"X": 100, "Y": 200, "Z": 300},
    }
    ctx.character = MagicMock()
    ctx.character.guid = "guid-123"
    ctx.player = MagicMock()
    ctx.player.unique_id = "76561198000000000"
    return ctx


def _make_rescue():
    rescue = MagicMock()
    rescue.character.name = "Requester"
    rescue.location.x = 100
    rescue.location.y = 195
    rescue.location.z = 300
    return rescue


def _make_point():
    point = MagicMock()
    point.name = "arena"
    point.location.x = 100
    point.location.y = 200
    point.location.z = 300
    return point


def _patches(rescues=(), points=()):
    return [
        patch(
            "amc.commands.teleport.TeleportPoint.objects.filter",
            return_value=_FakeQuerySet(points),
        ),
        patch(
            "amc.commands.teleport.TeleportPoint.objects.aget",
            new=AsyncMock(return_value=points[0] if points else None),
        ),
        patch(
            "amc.commands.teleport.PoliceSession.objects.filter",
            return_value=_FakeQuerySet([]),
        ),
        patch(
            "amc.commands.teleport.RescueRequest.objects.filter",
            return_value=_FakeQuerySet(rescues),
        ),
        patch("amc.commands.teleport.is_teleport_locked", new=AsyncMock(return_value=False)),
        patch(
            "amc.commands.teleport._check_police_tp_near_wanted",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "amc.commands.teleport.get_player_last_vehicle",
            new=AsyncMock(return_value={"vehicle": None}),
        ),
        patch("amc.commands.teleport.terrain_z_cm", return_value=-20_000),
    ]


def _assert_authoritative_strip(mock_tp, **expected):
    mock_tp.assert_awaited_once()
    assert mock_tp.await_args is not None
    kwargs = mock_tp.await_args.kwargs
    assert kwargs["remove_cargo"] is True, "bRemoveCargo selects ServerResetVehicleAt"
    for key, value in expected.items():
        assert kwargs[key] is value


@pytest.mark.asyncio
async def test_rescue_tp_uses_authoritative_reset_and_strips_cargo():
    """Rescue responders keep the vehicle but lose cargo, like /tp2marker."""
    ctx = _make_ctx(is_admin=False)

    with ExitStack() as stack:
        mock_tp = stack.enter_context(
            patch("amc.commands.teleport.teleport_player", new=AsyncMock())
        )
        for p in _patches(rescues=[_make_rescue()]):
            stack.enter_context(p)
        await cmd_tp_name(ctx, "")

    _assert_authoritative_strip(mock_tp, no_vehicles=False)


@pytest.mark.asyncio
async def test_admin_custom_tp_uses_authoritative_reset_and_strips_cargo():
    """Admin bare /tp is no longer a legacy client-side vehicle reset."""
    ctx = _make_ctx(is_admin=True)

    with ExitStack() as stack:
        mock_tp = stack.enter_context(
            patch("amc.commands.teleport.teleport_player", new=AsyncMock())
        )
        for p in _patches():
            stack.enter_context(p)
        await cmd_tp_name(ctx, "")

    _assert_authoritative_strip(mock_tp, no_vehicles=False)


@pytest.mark.asyncio
async def test_admin_named_tp_uses_authoritative_reset_and_strips_cargo():
    """Named-point /tp (the path that still despawned in the live test) is covered too."""
    ctx = _make_ctx(is_admin=True)

    with ExitStack() as stack:
        mock_tp = stack.enter_context(
            patch("amc.commands.teleport.teleport_player", new=AsyncMock())
        )
        for p in _patches(points=[_make_point()]):
            stack.enter_context(p)
        await cmd_tp_name(ctx, "arena")

    _assert_authoritative_strip(mock_tp, no_vehicles=False)
