"""Tests for the in-game /power command module (dispatch + rendering gates).

All logic under test is the powercalc library, which runs REAL on the
committed snapshot (same policy as the Discord cog tests); the command
handlers are exercised through registry.execute to cover the regex/optional
argument routing and the ordering vs the usage stubs.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from types import SimpleNamespace

from amc.command_framework import CommandContext, registry
from powercalc import ComputeResult

# power.compute_setup/search are patched at the CONSUMING module
import amc.commands.power as power_cmd


def _hit():
    return SimpleNamespace(
        peak_power_hp=400.0,
        peak_power_rpm=5200.0,
        peak_torque_nm=413.0,
        engine_part="SmallBlock_240HP",
        intake_part="201",
        turbo_part="Turbocharger_Stage1",
        category="car",
        cost=12345,
    )


@pytest.fixture
def ctx():
    ctx = MagicMock(spec=CommandContext)
    ctx.reply = AsyncMock()
    ctx.player_info = {}
    ctx.player = None
    ctx.character = MagicMock()
    ctx.character.guid = "guid-1"
    ctx.character.name = "Tester"
    return ctx


@pytest.mark.asyncio
async def test_bare_power_shows_guide(ctx):
    assert await registry.execute("/power", ctx) is True
    ctx.reply.assert_called_once()
    text = ctx.reply.await_args.args[0]
    assert "<Title>Power Calculator</>" in text
    for sub in ("/power setup", "/power recommend", "/power parts", "/power version"):
        assert sub in text


@pytest.mark.asyncio
async def test_setup_single_engine_routes(ctx):
    fake = MagicMock(spec=ComputeResult)
    with patch.object(power_cmd, "compute_setup", return_value=fake) as m:
        assert await registry.execute("/power setup SmallBlock_240HP", ctx) is True
    m.assert_called_once_with("SmallBlock_240HP", None, None)
    ctx.reply.assert_called_once()


@pytest.mark.asyncio
async def test_setup_with_intake_and_turbo_routes(ctx):
    fake = MagicMock(spec=ComputeResult)
    with patch.object(power_cmd, "compute_setup", return_value=fake) as m:
        assert (
            await registry.execute(
                "/power setup SmallBlock_240HP 201 Turbocharger_Stage1", ctx
            )
            is True
        )
    m.assert_called_once_with("SmallBlock_240HP", "201", "Turbocharger_Stage1")


@pytest.mark.asyncio
async def test_setup_bare_shows_usage_not_error(ctx):
    assert await registry.execute("/power setup", ctx) is True
    text = ctx.reply.await_args.args[0]
    assert "Usage" in text


@pytest.mark.asyncio
async def test_setup_part_not_found_replies_error(ctx):
    from powercalc import PartNotFound

    with patch.object(
        power_cmd, "compute_setup", side_effect=PartNotFound("Nope_404")
    ) as m:
        assert await registry.execute("/power setup Nope_404", ctx) is True
    m.assert_called_once()
    text = ctx.reply.await_args.args[0]
    assert "Unknown part" in text
    assert "Nope_404" in text


@pytest.mark.asyncio
async def test_setup_branch_looking_token_warns(ctx):
    # na/turbo/eco/ev is a recommend branch, not an intake id
    with patch.object(power_cmd, "compute_setup") as m:
        assert await registry.execute("/power setup SmallBlock_240HP na", ctx) is True
    m.assert_not_called()
    text = ctx.reply.await_args.args[0]
    assert "recommend" in text


@pytest.mark.asyncio
async def test_recommend_hp_only_fixed_limit(ctx):
    with patch.object(power_cmd, "search", return_value=[_hit()]) as m:
        assert await registry.execute("/power recommend 400", ctx) is True
    m.assert_called_once_with(
        400.0, tolerance=4.0, branch=None, min_mass=None, max_mass=None, limit=15
    )
    text = ctx.reply.await_args.args[0]
    assert "Builds near 400 hp" in text


@pytest.mark.asyncio
async def test_recommend_full_sequence(ctx):
    # hp, min weight, max weight, branch — the fixed sequence
    with patch.object(power_cmd, "search", return_value=[_hit()]) as m:
        assert (
            await registry.execute("/power recommend 400 900 1200 turbo", ctx) is True
        )
    m.assert_called_once_with(
        400.0, tolerance=4.0, branch="turbo", min_mass=900, max_mass=1200, limit=15
    )


@pytest.mark.asyncio
async def test_recommend_lone_branch_token(ctx):
    # branch can be given right after hp; weights omitted
    with patch.object(power_cmd, "search", return_value=[_hit()]) as m:
        assert await registry.execute("/power recommend 400 ev", ctx) is True
    m.assert_called_once_with(
        400.0, tolerance=4.0, branch="ev", min_mass=None, max_mass=None, limit=15
    )


@pytest.mark.asyncio
async def test_recommend_branch_is_case_insensitive(ctx):
    with patch.object(power_cmd, "search", return_value=[_hit()]) as m:
        assert await registry.execute("/power recommend 400 TURBO", ctx) is True
    assert m.call_args.kwargs["branch"] == "turbo"


@pytest.mark.asyncio
async def test_recommend_any_maps_to_all(ctx):
    with patch.object(power_cmd, "search", return_value=[_hit()]) as m:
        assert await registry.execute("/power recommend 400 any", ctx) is True
    assert m.call_args.kwargs["branch"] == "all"


@pytest.mark.asyncio
async def test_recommend_unknown_branch_replies_error(ctx):
    with patch.object(power_cmd, "search") as m:
        assert await registry.execute("/power recommend 400 diesel", ctx) is True
    m.assert_not_called()
    assert "not an induction type" in ctx.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_recommend_lone_weight_replies_error(ctx):
    # weights must come as a pair
    with patch.object(power_cmd, "search") as m:
        assert await registry.execute("/power recommend 400 900", ctx) is True
    m.assert_not_called()
    assert "minimum AND maximum" in ctx.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_recommend_bare_shows_usage(ctx):
    assert await registry.execute("/power recommend", ctx) is True
    text = ctx.reply.await_args.args[0]
    assert "Usage" in text
    assert "/power recommend <hp>" in text


@pytest.mark.asyncio
async def test_recommend_empty_results_replies_hint(ctx):
    with patch.object(power_cmd, "search", return_value=[]):
        assert await registry.execute("/power recommend 400", ctx) is True
    text = ctx.reply.await_args.args[0]
    assert "No builds near 400 hp" in text


@pytest.mark.asyncio
async def test_parts_replies_intakes_and_turbos(ctx):
    fake = {
        "Intake": {"201": {"intake": {"Slope": 0.1, "BaseRPMRatio": 0.8}}},
        "Turbocharger": {
            "Turbocharger_Stage1": {"turbocharger": {"TorqueMultiplier": 1.4}},
            "Turbocharger_Eco1": {"turbocharger": {"TorqueMultiplier": 1.1}},
        },
    }
    with patch.object(power_cmd, "list_parts", return_value=fake):
        assert await registry.execute("/power parts", ctx) is True
    text = ctx.reply.await_args.args[0]
    assert "Intakes" in text and "201" in text
    assert "Turbocharger_Stage1" in text
    assert "(reduced hp)" in text  # Eco turbo note


@pytest.mark.asyncio
async def test_version_replies_model_and_data(ctx):
    fake_prov = {
        "validation": {
            "method": "dyno sweep",
            "in_game": {
                "peak_torque_nm": 413.2,
                "peak_torque_rpm": 3600,
                "peak_power_hp": 293.1,
                "peak_power_rpm": 5200,
            },
            "model": {
                "peak_torque_nm": 413.0,
                "peak_torque_rpm": 3600,
                "peak_power_hp": 293.0,
                "peak_power_rpm": 5200,
            },
        }
    }
    with (
        patch.object(power_cmd, "provenance", return_value=fake_prov),
        patch.object(power_cmd, "model_version", return_value="mv1"),
        patch.object(power_cmd, "data_version", return_value="dv1"),
    ):
        assert await registry.execute("/power version", ctx) is True
    text = ctx.reply.await_args.args[0]
    assert "mv1" in text and "dv1" in text
    assert "293.0" in text and "413.2" in text
