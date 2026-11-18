"""teleport_player must request the mod's Force bypass by default.

Motor Town 0.7.20 rejects ``ServerTeleportCharacter`` calls that arrive
without a charge (logged game-side as ``Rejected no-charge
ServerTeleportCharacter``), which silently killed every backend-driven
character teleport. The mod's ``Force`` branch
(``K2_SetActorLocation`` + ``ClientTeleportedCharacter``) is the way around it,
so the payload must carry it unless a caller explicitly opts out.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from amc import mod_server


class _Resp:
    status = 200

    async def text(self):
        return ""


class _Post:
    def __init__(self, captured):
        self.captured = captured

    def __call__(self, url, json=None):
        self.captured["url"] = url
        self.captured["json"] = json
        return self

    async def __aenter__(self):
        return _Resp()

    async def __aexit__(self, *exc):
        return False


def _session(captured):
    session = MagicMock()
    session.post = _Post(captured)
    return session


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    monkeypatch.setattr(mod_server._write_limiter, "acquire", AsyncMock())


@pytest.mark.asyncio
async def test_teleport_player_sends_force_by_default():
    captured = {}
    await mod_server.teleport_player(
        _session(captured), "7656119", {"X": 1, "Y": 2, "Z": 3}
    )
    assert captured["url"] == "/players/7656119/teleport"
    assert captured["json"]["Force"] is True
    assert captured["json"]["Location"] == {"X": 1, "Y": 2, "Z": 3}


@pytest.mark.asyncio
async def test_teleport_player_can_opt_out_of_force():
    captured = {}
    await mod_server.teleport_player(
        _session(captured), "7656119", {"X": 1, "Y": 2, "Z": 3}, force=False
    )
    assert "Force" not in captured["json"]


@pytest.mark.asyncio
async def test_teleport_player_keeps_other_flags():
    captured = {}
    await mod_server.teleport_player(
        _session(captured),
        "7656119",
        {"X": 1, "Y": 2, "Z": 3},
        no_vehicles=True,
        reset_trailers=True,
        reset_carried_vehicles=True,
        remove_cargo=False,
    )
    payload = captured["json"]
    assert payload["Force"] is True
    assert payload["NoVehicles"] is True
    assert payload["bResetTrailers"] is True
    assert payload["bResetCarriedVehicles"] is True
    assert payload["bRemoveCargo"] is False
