"""Persistent mute helpers for the Player model.

Mutes enforced by the Motor Town mod live only in the mod's in-memory
``mutedPlayers`` table (MTDediMod/Scripts/PlayerManager.lua) and are wiped on
every server restart. To make a mute survive a restart WITHOUT any mod-side
change, the backend persists the mute on ``amc_player.muted_until`` and
re-applies it through the mod HTTP API whenever the player logs in.

Semantics of ``Player.muted_until``:
  - ``None``                    → not muted.
  - ``PERMANENT_MUTE_UNTIL``    → permanent mute (sentinel far-future value).
  - any other datetime          → temporary mute, expires at that instant.

Re-application is idempotent and cheap: the mod endpoint stores
``mutedPlayers[uniqueId]`` keyed by steam id, so re-muting an already-muted
player just refreshes the expiry. It is safe to call on every login.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta, timezone as dt_timezone

from django.utils import timezone

logger = logging.getLogger(__name__)

# Sentinel marking a permanent mute. Postgres timestamptz stores year 9999
# fine; we detect it by ``year >= 9999`` on re-read to avoid any microsecond /
# timezone drift from the round-trip.
PERMANENT_MUTE_UNTIL = timezone.datetime(
    9999, 12, 31, 23, 59, 59, tzinfo=dt_timezone.utc
)


async def persist_mute(player, mute_for, hard=True):
    """Persist a mute on the Player row so it survives a server restart.

    ``mute_for``: ``True`` → permanent; a positive int → relative seconds.
    Does NOT touch the live mod session — callers (admin commands) already
    apply the mute live; this only records the durable intent.
    """
    if mute_for is True:
        player.muted_until = PERMANENT_MUTE_UNTIL
    else:
        player.muted_until = timezone.now() + timedelta(seconds=int(mute_for))
    await player.asave(update_fields=["muted_until"])
    return player.muted_until


async def clear_persistent_mute(player):
    """Drop a persisted mute (set muted_until back to None)."""
    player.muted_until = None
    await player.asave(update_fields=["muted_until"])


def is_muted(player) -> bool:
    """Return True if a player is currently muted (persisted mute active).

    None → not muted. ``PERMANENT_MUTE_UNTIL`` (year >= 9999) → muted forever.
    Any other future datetime → muted until then. Expired temporary mutes
    count as NOT muted (they'll be cleared by ``reapply_mute_on_login``).
    """
    until = player.muted_until if player else None
    if until is None:
        return False
    if until.year >= 9999:
        return True
    return until > timezone.now()


async def reapply_mute_on_login(player, http_client_mod):
    """Ensure a player with a persisted mute is muted in the live mod session.

    Called on every character login for that player. Prunes an expired
    temporary mute (sets muted_until back to None) and otherwise pushes the
    mute to the mod. Safe/fire-and-forget: failures degrade to a logged
    no-op so login is never gated on the mute.
    """
    until = player.muted_until
    if until is None:
        return

    from amc.mod_server import mute_player  # avoid import cycle at module load

    try:
        if until.year >= 9999:
            # Permanent.
            await mute_player(
                http_client_mod, player.unique_id, mute_for=True, hard=True
            )
            return

        now = timezone.now()
        if until <= now:
            # Temporary mute has expired — clear the persisted flag.
            player.muted_until = None
            await player.asave(update_fields=["muted_until"])
            logger.info(
                "Persistent mute for %s (%s) expired; cleared.",
                player.unique_id,
                until.isoformat(),
            )
            return

        remaining = int(until.timestamp()) - int(now.timestamp())
        await mute_player(
            http_client_mod, player.unique_id, mute_for=remaining, hard=True
        )
        logger.info(
            "Re-applied persistent mute for %s (%.0fs left).",
            player.unique_id,
            remaining,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - login must never fail on mute
        logger.warning(
            "Failed to re-apply persistent mute for %s: %s", player.unique_id, e
        )
