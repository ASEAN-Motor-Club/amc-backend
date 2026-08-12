"""Scheduled pinned-announcement driver.

The Django admin is the source of truth (see ``ScheduledAnnouncement``). This
module computes the currently-live pinned message and pushes it to the game
server mod's ``POST /pin`` endpoint. The mod holds no scheduling/recurrence
logic — it just writes whatever value it receives to ``PinnedAnnounce`` (the
``/ap`` board).
"""

import logging
import time

from django.core.cache import cache
from django.utils import timezone

from amc.models import ScheduledAnnouncement

logger = logging.getLogger(__name__)

_LAST_MSG_KEY = "pinned_announce:last_message"
_LAST_TS_KEY = "pinned_announce:last_push_ts"
# Re-assert the pin at least this often (seconds) so it self-heals after a
# server restart even when the message hasn't changed.
_STALE_AFTER = 5 * 60


async def current_pinned_message() -> str:
    """Return the currently-live pinned announcement message ("" if none)."""
    now = timezone.now()
    best_message = ""
    best_occurrence = None

    entries = [
        entry async for entry in ScheduledAnnouncement.objects.filter(enabled=True)
    ]
    for entry in entries:
        occurrence = entry.latest_occurrence(now)
        if occurrence is None:
            continue
        if best_occurrence is None or occurrence > best_occurrence:
            best_occurrence = occurrence
            best_message = entry.message

    return best_message


async def drive_pinned_announcement(ctx):
    """arq cron: push the current pinned message to the mod's POST /pin.

    The mod holds no logic — it just writes the value it receives to the `/ap`
    board. This driver pushes only when:
      * the message changed since the last successful push, or
      * the last successful push is stale (covers server restarts, where the
        board is wiped on boot).

    Pushing an empty message clears the board (admin disabled the entry).
    """
    mod_session = ctx.get("http_client_mod")
    if mod_session is None:
        return

    from amc.mod_server import set_pinned_announcement

    message = await current_pinned_message()
    last_message = cache.get(_LAST_MSG_KEY)
    last_ts = cache.get(_LAST_TS_KEY, 0.0)

    now_ts = time.time()
    changed = last_message != message
    stale = (now_ts - last_ts) > _STALE_AFTER
    if not changed and not stale:
        return

    try:
        await set_pinned_announcement(mod_session, message)
    except Exception:  # noqa: BLE001 — push is best-effort; cron retries next tick
        logger.exception("Failed to push pinned announcement to mod (%r)", message)
        return

    cache.set(_LAST_MSG_KEY, message)
    cache.set(_LAST_TS_KEY, now_ts)
