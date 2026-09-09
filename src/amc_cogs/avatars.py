"""Shared Discord-avatar helpers for rendered cards.

Used by the leaderboard gov-board card and the daily government employee
report. Avatars are fetched via the bot (24h in-process cache), normalized
to AVATAR_SIZE (Discord's CDN ignores ?size= for default avatars and serves
them at 256x256), and masked to an antialiased circle for matplotlib
OffsetImage compositing.
"""

import asyncio
import io
import logging
import time

logger = logging.getLogger(__name__)

AVATAR_CACHE_TTL = 24 * 3600.0
AVATAR_SIZE = 128


def circle_rgba(png_bytes: bytes):
    """Decode avatar bytes, normalize to AVATAR_SIZE, apply an antialiased circular alpha mask.

    The Discord CDN ignores ?size= for default avatars and serves them at
    256x256, so every image is resized to AVATAR_SIZE before masking —
    OffsetImage's fixed zoom would otherwise render default avatars 2x too
    large.
    """
    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    if img.width != AVATAR_SIZE or img.height != AVATAR_SIZE:
        img = img.resize((AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS)
    arr = np.asarray(img, dtype=float) / 255.0
    h, w = arr.shape[:2]
    n = min(h, w)
    yy, xx = np.mgrid[0:h, 0:w]
    d = np.sqrt((yy - (h - 1) / 2) ** 2 + (xx - (w - 1) / 2) ** 2)
    alpha = np.clip(n / 2 - d + 0.5, 0.0, 1.0)
    out = arr.copy()
    out[..., 3] = np.minimum(out[..., 3], alpha)
    return out


def placeholder_rgba():
    """Neutral 'no avatar' disc for unlinked players / failed fetches."""
    import numpy as np

    n = AVATAR_SIZE
    yy, xx = np.mgrid[0:n, 0:n]
    d = np.sqrt((yy - (n - 1) / 2) ** 2 + (xx - (n - 1) / 2) ** 2)
    alpha = np.clip(n / 2 - d + 0.5, 0.0, 1.0)
    rgb = np.zeros((n, n, 3))
    rgb[..., 0] = 0.29
    rgb[..., 1] = 0.31
    rgb[..., 2] = 0.35
    return np.dstack([rgb, alpha])


async def fetch_avatar_bytes(
    bot, discord_user_id: int, cache: dict[int, tuple[float, bytes]]
) -> bytes | None:
    """Fetch a Discord user's avatar PNG bytes, cached for 24h."""
    now = time.monotonic()
    cached = cache.get(discord_user_id)
    if cached is not None and now - cached[0] < AVATAR_CACHE_TTL:
        return cached[1]
    try:
        user = bot.get_user(discord_user_id)
        if user is None:
            user = await bot.fetch_user(discord_user_id)
        data = await user.display_avatar.with_size(AVATAR_SIZE).with_format(
            "png"
        ).read()
    except Exception:
        logger.warning(
            "Avatar fetch failed for discord user %s",
            discord_user_id,
            exc_info=True,
        )
        return None
    cache[discord_user_id] = (now, data)
    return data


async def get_avatar_rgba(bot, discord_user_id: int | None, cache):
    """Masked RGBA avatar array, or None when unlinked / fetch failed."""
    if not discord_user_id:
        return None
    data = await fetch_avatar_bytes(bot, discord_user_id, cache)
    if data is None:
        return None
    try:
        return await asyncio.to_thread(circle_rgba, data)
    except Exception:
        logger.warning(
            "Avatar decode failed for discord user %s",
            discord_user_id,
            exc_info=True,
        )
        return None
