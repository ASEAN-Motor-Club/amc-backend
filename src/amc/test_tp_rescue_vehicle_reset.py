"""Rescue /tp must move the player's vehicle server-authoritatively.

A rescue responder keeps their vehicle (no_vehicles=False). If the backend
omits bRemoveCargo, the mod falls back to its legacy client-side
ClientResetVehicleAt RPC; the game rejects the resulting
ServerResetVehicleAtResponse as unsolicited, so the vehicle is never moved
authoritatively and despawns when the driver exits. Sending bRemoveCargo
selects the server-authoritative ServerResetVehicleAt branch, which keeps
cargo attached when the value is False.
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


def _patches(rescues=()):
    return [
        patch("amc.commands.teleport.TeleportPoint.objects.filter", return_value=_FakeQuerySet([])),
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


@pytest.mark.asyncio
async def test_rescue_tp_keeps_cargo_but_uses_authoritative_vehicle_reset():
    """Rescue responders get bRemoveCargo=False so the mod moves the vehicle server-side."""
    ctx = _make_ctx(is_admin=False)

    with ExitStack() as stack:
        mock_tp = stack.enter_context(
            patch("amc.commands.teleport.teleport_player", new=AsyncMock())
        )
        for p in _patches(rescues=[_make_rescue()]):
            stack.enter_context(p)
        await cmd_tp_name(ctx, "")

    mock_tp.assert_awaited_once()
    assert mock_tp.await_args is not None
    kwargs = mock_tp.await_args.kwargs
    assert kwargs["no_vehicles"] is False, "rescue responders must keep their vehicle"
    assert kwargs["remove_cargo"] is False, (
        "bRemoveCargo=False selects ServerResetVehicleAt, keeping cargo attached"
    )


@pytest.mark.asyncio
async def test_non_rescue_custom_tp_keeps_existing_vehicle_behaviour():
    """Admin custom-destination /tp must not gain a bRemoveCargo field."""
    ctx = _make_ctx(is_admin=True)

    with ExitStack() as stack:
        mock_tp = stack.enter_context(
            patch("amc.commands.teleport.teleport_player", new=AsyncMock())
        )
        for p in _patches():
            stack.enter_context(p)
        await cmd_tp_name(ctx, "")

    mock_tp.assert_awaited_once()
    assert mock_tp.await_args.kwargs["remove_cargo"] is None
