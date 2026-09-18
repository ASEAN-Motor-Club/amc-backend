"""Tests for the AMC driver's license card renderer (pure PIL, no DB)."""

import io
from datetime import date
from importlib import resources

from PIL import Image

from amc_cogs.license_card import card_number, render_license_card


def _emblem_bytes() -> bytes:
    return (resources.files("amc_cogs") / "assets" / "amc_emblem.png").read_bytes()


def test_render_returns_png_with_expected_dimensions():
    png = render_license_card(
        name="Meehoi San",
        discord_id="1155069673512120341",
        issued=date(2026, 9, 18),
        joined=date(2024, 3, 2),
        logo_bytes=_emblem_bytes(),
        avatar_bytes=None,
    )
    img = Image.open(io.BytesIO(png))
    assert img.size == (1012, 638)
    assert img.format == "PNG"


def test_card_number_is_deterministic():
    assert card_number("1155069673512120341") == card_number("1155069673512120341")
    assert card_number("1") != card_number("2")
    assert len(card_number("123").replace("-", "")) == 10
    assert "-" in card_number("123")


def test_render_with_placeholder_avatar_when_none():
    png = render_license_card(
        name="X" * 30,
        discord_id="42",
        issued=date(2026, 1, 1),
        joined=None,
        logo_bytes=_emblem_bytes(),
        avatar_bytes=None,
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_with_real_avatar_bytes():
    # build a solid-color avatar at 256px (Discord CDN default-avatar size)
    src = Image.new("RGBA", (256, 256), (200, 30, 30, 255))
    buf = io.BytesIO()
    src.save(buf, "PNG")
    png = render_license_card(
        name="Wickedhaze",
        discord_id="461537532383985665",
        issued=date(2026, 9, 18),
        joined=date(2025, 1, 15),
        logo_bytes=_emblem_bytes(),
        avatar_bytes=buf.getvalue(),
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_with_garbage_avatar_bytes_falls_back_to_placeholder():
    png = render_license_card(
        name="Broken Avatar",
        discord_id="99",
        issued=date(2026, 9, 18),
        joined=date(2025, 1, 15),
        logo_bytes=_emblem_bytes(),
        avatar_bytes=b"not a png",
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_corners_are_transparent():
    png = render_license_card(
        name="Corner Check",
        discord_id="7",
        issued=date(2026, 9, 18),
        joined=date(2024, 3, 2),
        logo_bytes=_emblem_bytes(),
        avatar_bytes=None,
    )
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    assert img.getpixel((0, 0))[3] == 0
    assert img.getpixel((1011, 0))[3] == 0
    assert img.getpixel((0, 637))[3] == 0
    assert img.getpixel((1011, 637))[3] == 0


def test_long_name_is_rendered_without_overflow():
    # 40-char name must not raise and must shrink the font (no exception = pass;
    # output is still a valid card)
    png = render_license_card(
        name="A" * 40,
        discord_id="8",
        issued=date(2026, 9, 18),
        joined=None,
        logo_bytes=_emblem_bytes(),
        avatar_bytes=None,
    )
    assert Image.open(io.BytesIO(png)).size == (1012, 638)
