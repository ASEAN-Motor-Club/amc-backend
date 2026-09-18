"""AMC Driver's License card renderer.

Pure-PIL port of the approved card design (1012x638, CR80 credit-card ratio
at 300 DPI). No DB or Discord objects — takes prepared values, returns PNG
bytes, so tests call it directly.

Card layout (from the approved mock):
- dark navy diagonal gradient background with subtle sheen
- header: ASEAN MOTOR CLUB / DRIVER'S LICENSE
- ASEAN emblem top-right inside a white ring
- circular avatar in an amber ring on the left
- driver name, card NO. (deterministic from Discord ID), Discord ID
- DATE OF ISSUE / MEMBER SINCE side by side
- footer: CLASS: ALL VEHICLES / ASEAN MOTOR CLUB - MOTOR TOWN + amber stripe
"""

from __future__ import annotations

import hashlib
import io
import os
from datetime import date

from PIL import Image, ImageDraw, ImageFilter, ImageFont

CARD_W, CARD_H = 1012, 638
CORNER_RADIUS = 34

# palette (from the approved SVG mock)
_BG_TOP = (16, 24, 32)  # #101820
_BG_MID = (22, 40, 59)  # #16283b
_BG_BOT = (11, 17, 22)  # #0b1116
_AMBER = (244, 163, 0)  # #f4a300
_AMBER_LIGHT = (255, 207, 92)  # #ffcf5c
_LABEL = (143, 163, 184)  # #8fa3b8
_DIM = (107, 127, 148)  # #6b7f94
_WHITE = (255, 255, 255)
_RULE = (42, 58, 77)  # #2a3a4d

_AVATAR_DIAMETER = 236
_AVATAR_CENTER = (238, 352)
_LOGO_CENTER = (900, 128)
_LOGO_DIAMETER = 132  # emblem circle inside the 148px white ring

_DEJAVU = "/usr/share/fonts/truetype/dejavu/"
# Bundled fallback fonts — the prod nix env has no system fonts
# (verified: no /usr/share/fonts on the host, no TTFs in the env closure).
_FONT_DIR = os.path.join(os.path.dirname(__file__), "assets", "fonts")


def card_number(discord_id: str) -> str:
    """Deterministic card NO. from the Discord ID: XXXX-XXXXXX (10 hex)."""
    hexd = hashlib.sha256(discord_id.encode()).hexdigest()[:10].upper()
    return f"{hexd[:4]}-{hexd[4:]}"


def _load_font(size: int, mono: bool = False):
    name = "DejaVuSansMono-Bold.ttf" if mono else "DejaVuSans-Bold.ttf"
    for base in (_DEJAVU, _FONT_DIR):
        try:
            return ImageFont.truetype(os.path.join(base, name), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _load_font_regular(size: int):
    for base in (_DEJAVU, _FONT_DIR):
        try:
            return ImageFont.truetype(os.path.join(base, "DejaVuSans.ttf"), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _vertical_gradient(size, stops):
    """3-stop vertical gradient image."""
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    seg = h // 2
    for y in range(h):
        if y < seg:
            t = y / max(seg - 1, 1)
            a, b = stops[0], stops[1]
        else:
            t = (y - seg) / max(h - seg - 1, 1)
            a, b = stops[1], stops[2]
        color = tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[index]
        for x in range(w):
            px[x, y] = color  # type: ignore[index]
    return img


def _rounded_mask(size, radius) -> Image.Image:
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1],
                                        radius=radius, fill=255)
    return m


def _circle_clip(img: Image.Image, center, diameter) -> Image.Image:
    """Return a circularly-clipped RGBA copy of img resized to diameter."""
    img = img.convert("RGBA").resize((diameter, diameter), Image.LANCZOS)
    mask = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, diameter - 1, diameter - 1], fill=255)
    out = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _placeholder_avatar() -> Image.Image:
    img = Image.new("RGBA", (_AVATAR_DIAMETER, _AVATAR_DIAMETER), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, _AVATAR_DIAMETER - 1, _AVATAR_DIAMETER - 1],
              fill=(96, 112, 88, 255))  # muted olive disc
    # simple silhouette, clipped to the disc
    cx = _AVATAR_DIAMETER // 2
    d.ellipse([cx - 42, 64, cx + 42, 148], fill=(255, 255, 255, 230))
    d.ellipse([cx - 78, 152, cx + 78, 320], fill=(255, 255, 255, 255))
    mask = Image.new("L", (_AVATAR_DIAMETER, _AVATAR_DIAMETER), 0)
    ImageDraw.Draw(mask).ellipse(
        [0, 0, _AVATAR_DIAMETER - 1, _AVATAR_DIAMETER - 1], fill=255)
    out = Image.new("RGBA", (_AVATAR_DIAMETER, _AVATAR_DIAMETER), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def render_license_card(
    name: str,
    discord_id: str,
    issued: date,
    joined: date | None,
    logo_bytes: bytes,
    avatar_bytes: bytes | None,
    gov_level: int | None = None,
) -> bytes:
    """Render the license card to PNG bytes.

    joined=None renders as an em-dash (data missing, not "Unknown").
    gov_level >= 50 switches to the gold Government Official theme with a
    "GOV LEVEL" field.
    """
    gov = gov_level is not None and gov_level >= 50

    # theme: standard (navy/amber) vs government official (dark gold/black)
    if gov:
        bg_stops = ((24, 20, 6), (54, 44, 12), (14, 12, 4))
        accent = (255, 191, 0)  # richer gold
        accent_light = (255, 226, 120)
        title = "GOVERNMENT OFFICIAL"
    else:
        bg_stops = (_BG_TOP, _BG_MID, _BG_BOT)
        accent = _AMBER
        accent_light = _AMBER_LIGHT
        title = "DRIVER'S LICENSE"

    card = _vertical_gradient((CARD_W, CARD_H), bg_stops)
    # subtle diagonal sheen: overlay a soft light band
    sheen = Image.new("L", (CARD_W, CARD_H), 0)
    sd = ImageDraw.Draw(sheen)
    sd.polygon([(0, 0), (CARD_W // 2, 0), (CARD_W // 3, CARD_H), (0, CARD_H)],
               fill=14)
    sheen = sheen.filter(ImageFilter.GaussianBlur(60))
    card = Image.composite(Image.new("RGB", card.size, (255, 255, 255)), card, sheen)
    draw = ImageDraw.Draw(card)

    # --- header ---
    draw.text((56, 60), "ASEAN MOTOR CLUB", font=_load_font(26), fill=_LABEL)
    # letter-spacing approximation: redraw spaced
    f_title = _load_font(40)
    draw.text((56, 92), title, font=f_title, fill=accent)

    # --- logo: white ring + emblem ---
    rx, ry = _LOGO_CENTER
    r_ring = 74
    draw.ellipse([rx - r_ring, ry - r_ring, rx + r_ring, ry + r_ring],
                 fill=(255, 255, 255, 235))
    emblem = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
    clipped = _circle_clip(emblem, _LOGO_CENTER, _LOGO_DIAMETER)
    card.paste(clipped, (rx - _LOGO_DIAMETER // 2, ry - _LOGO_DIAMETER // 2),
               clipped)

    # --- avatar: amber ring + circular avatar ---
    ax, ay = _AVATAR_CENTER
    ar = 124
    draw.ellipse([ax - ar, ay - ar, ax + ar, ay + ar], fill=accent)
    if avatar_bytes:
        try:
            av = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            # normalize to the window size (Discord CDN size quirks)
            av = _circle_clip(av, (ax, ay), _AVATAR_DIAMETER)
        except Exception:  # noqa: BLE001 — bad avatar bytes must never block the card
            av = _placeholder_avatar()
    else:
        av = _placeholder_avatar()
    card.paste(av, (ax - _AVATAR_DIAMETER // 2, ay - _AVATAR_DIAMETER // 2), av)
    # redraw the ring above the avatar so the circle frame stays intact
    draw.ellipse([ax - ar, ay - ar, ax + ar, ay + ar], outline=accent, width=10)

    # --- fields ---
    fx = 420
    f_label = _load_font_regular(22)
    f_mono = _load_font(20, mono=True)
    f_date = _load_font(30, mono=True)

    draw.text((fx, 196), "DRIVER NAME", font=f_label, fill=_LABEL)
    # auto-shrink long names
    size = 54
    f_name = _load_font(size)
    while size > 28:
        f_name = _load_font(size)
        bbox = draw.textbbox((0, 0), name, font=f_name)
        if bbox[2] - bbox[0] <= 530:
            break
        size -= 4
    draw.text((fx, 224), name, font=f_name, fill=_WHITE)
    draw.rectangle([fx, 288, fx + 530, 290], fill=_RULE)

    draw.text((fx, 316), f"NO. {card_number(discord_id)}", font=f_mono, fill=_LABEL)
    draw.text((fx, 346), f"DISCORD ID: {discord_id}", font=f_mono, fill=_DIM)

    draw.text((fx, 412), "DATE OF ISSUE", font=f_label, fill=_LABEL)
    draw.text((fx, 440), issued.isoformat(), font=f_date, fill=_WHITE)
    draw.text((700, 412), "MEMBER SINCE", font=f_label, fill=_LABEL)
    draw.text((700, 440), (joined.isoformat() if joined else "—"),
              font=f_date, fill=_WHITE)

    # --- gov level badge (government theme only) ---
    if gov:
        f_badge = _load_font(22, mono=True)
        badge_text = f"GOV LEVEL {gov_level}"
        bbox = draw.textbbox((0, 0), badge_text, font=f_badge)
        bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        bx, by = 700, 492
        draw.rounded_rectangle([bx - 14, by - 10, bx + bw + 14, by + bh + 16],
                               radius=8, fill=accent)
        draw.text((bx, by), badge_text, font=f_badge, fill=(24, 18, 2))

    # --- footer ---
    draw.text((56, CARD_H - 62), "CLASS: ALL VEHICLES",
              font=_load_font(18, mono=True), fill=_LABEL)
    draw.text((560, CARD_H - 62), "ASEAN MOTOR CLUB — MOTOR TOWN",
              font=_load_font(18, mono=True), fill=_DIM)
    # bottom stripe (gradient left→right)
    for x in range(CARD_W):
        t = x / (CARD_W - 1)
        color = tuple(int(accent[i] + (accent_light[i] - accent[i]) * t) for i in range(3))
        draw.line([(x, CARD_H - 26), (x, CARD_H - 1)], fill=color)

    # rounded-corner mask
    out = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    out.paste(card, (0, 0), _rounded_mask((CARD_W, CARD_H), CORNER_RADIUS))

    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()
