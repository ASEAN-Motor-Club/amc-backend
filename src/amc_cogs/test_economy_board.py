"""Tests for the economy board cog (pure helpers, no Discord I/O)."""

from amc_cogs.economy_board import (
    _int_color,
    build_board_embed,
    health_color,
    render_board_png,
)

SECTORS = [
    {
        "sector": "construction",
        "amount": 40,
        "capacity": 2000,
        "fill": 0.02,
        "starved_sites": 222,
    },
    {
        "sector": "mining",
        "amount": 5200,
        "capacity": 10000,
        "fill": 0.52,
        "starved_sites": 0,
    },
]

CONTRIBUTORS = [
    {"name": "NiSSiX", "units": 1200, "payment": 5_000_000, "score": 7_900_000},
    {"name": "Test310", "units": 900, "payment": 3_000_000, "score": 6_490_000},
]


def test_health_color_bands():
    assert health_color(0.7) == "#3fb950"
    assert health_color(0.45) == "#d29922"
    assert health_color(0.1) == "#f85149"
    assert _int_color(0.7) == 0x3FB950
    assert _int_color(0.1) == 0xF85149


def test_build_board_embed_overall_and_most_needed():
    embed = build_board_embed(SECTORS, CONTRIBUTORS)
    assert embed.title == "State of the Economy"
    desc = embed.description or ""
    # unit-weighted overall = (40+5200)/(2000+10000) = 43.5% -> 44%
    assert "44%" in desc
    assert "Construction" in desc  # starved sector surfaced
    assert (embed.image.url or "").endswith("economy_board.png")
    assert embed.color is not None and int(embed.color) == 0xD29922


def test_render_board_png_returns_png_bytes():
    out = render_board_png(SECTORS, CONTRIBUTORS, "12:00 UTC")
    assert out is not None
    assert out[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_board_png_skips_when_no_data():
    assert render_board_png([], CONTRIBUTORS, "12:00 UTC") is None
    no_fill = [dict(s, fill=None) for s in SECTORS]
    assert render_board_png(no_fill, CONTRIBUTORS, "12:00 UTC") is None
