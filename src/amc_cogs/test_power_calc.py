"""Tests for the /power cog — rendering helpers + command wiring.

No DB needed: the cog is a thin layer over powercalc. The interaction
callbacks are exercised through the embed helpers; autocomplete and the
cog registration are smoke-tested.
"""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from amc_cogs.power_calc import (
    PowerCalcCog,
    _curve_block,
    _filter_choices,
    _recommend_embed,
    _setup_embed,
    _turbo_autocomplete,
)
from powercalc import compute_setup, search


@pytest.fixture
def cog():
    bot = MagicMock()
    return PowerCalcCog(bot)


def test_cog_group_name(cog):
    assert cog.power.name == "power"
    subcommands = {cmd.name for cmd in cog.power.commands}
    assert {"setup", "recommend", "parts", "version"} <= subcommands


def test_setup_embed_fields():
    res = compute_setup("SmallBlock_240HP", "201", "Turbocharger_Stage1")
    emb = _setup_embed(res)
    desc = emb.description
    assert "293.2" in desc or "293" in desc  # golden dyno value
    assert "413" in desc  # peak torque Nm
    assert "SmallBlock_240HP" in desc
    assert "201 (stock)" not in desc  # 201 is a real part, not stock
    assert "Turbocharger_Stage1" in desc
    assert emb.footer.text and "data" in emb.footer.text  # footer carries versions


def test_setup_embed_ev():
    res = compute_setup("EVPolestar2DualMotor")
    emb = _setup_embed(res)
    assert "EV" in emb.title or "EV" in emb.description
    assert "412" in emb.description


def test_curve_block_renders():
    res = compute_setup(
        "SmallBlock_240HP", "201", "Turbocharger_Stage1", keep_points=True
    )
    block = _curve_block(res)
    assert block.startswith("```")
    assert "T" in block and "P" in block
    assert "rpm" in block


def test_recommend_embed_empty_and_filled():
    empty = _recommend_embed(400, [])
    assert "No builds" in empty.title

    hits = search(
        400, tolerance=4.0, categories=["car"], min_mass=100, max_mass=600, limit=5
    )
    emb = _recommend_embed(400, hits)
    assert "400" in emb.title
    assert "30tdi" in emb.description or len(hits) == 5
    for h in hits:
        assert f"{h.peak_power_hp:.1f} hp" in emb.description


def test_recommend_respects_branch_choice_value():
    hits = search(400.0, tolerance=4.0, branch="eco", limit=5)
    assert hits
    assert all(h.turbo_part.startswith("Turbocharger_Eco") for h in hits)


def test_filter_choices_matching_and_cap():
    items = [(f"part{i}", f"Nice part {i}") for i in range(40)]
    out = _filter_choices(items, "")
    assert len(out) == 25  # discord cap
    out = _filter_choices(items, "part7")
    assert len(out) == 1  # exact value hit; "part 7" (name) has a space
    out = _filter_choices(items, "part3")
    assert len(out) == 11  # part3 + part30..part39
    assert all(isinstance(c, discord.app_commands.Choice) for c in out)


def test_turbo_autocomplete_labels_eco():
    import asyncio

    choices = asyncio.run(_turbo_autocomplete(MagicMock(), "Eco"))
    assert choices
    assert all("(reduced hp)" in c.name for c in choices)


def test_parts_command_sends_embed_as_kwarg(cog):
    """Regression: the embed must go as send_message(embed=...), never
    positionally. A positional Embed lands in the `content` slot and
    discord.py stringifies it (str(Embed) is the object repr), so the
    user gets a garbage ephemeral message with no embed — the bug that
    shipped in the original /power parts (#68)."""
    import asyncio

    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    asyncio.run(PowerCalcCog.power_parts.callback(cog, interaction))

    interaction.response.send_message.assert_called_once()
    kwargs = interaction.response.send_message.await_args.kwargs
    emb = kwargs.get("embed")
    assert isinstance(emb, discord.Embed)
    assert kwargs.get("ephemeral") is True
    assert emb.description and "Intakes" in emb.description
    assert "Turbos" in emb.description
