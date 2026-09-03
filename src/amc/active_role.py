"""Daily sync of the "Active" Discord role.

A linked player (Player.discord_user_id set) whose most recent character
login — MAX(lower(PlayerStatusLog.timespan)) over all their characters, the
Player-level lift of CharacterQuerySet.with_last_login — is within
ACTIVE_WINDOW_DAYS holds the configured Discord role; everyone else holding
it loses it. Stateless diff against the live Discord role membership, so
the job self-heals (no DB record of who has the role).

Runs as an arq cron job (see amc_backend/worker.py). The Discord work must
execute on the bot's event loop — the client lives in a worker thread — so
the cron entry dispatches via run_coroutine_threadsafe (same cross-thread
pattern as amc/tasks.py's message queue).
"""

import asyncio
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from amc.models import Player

logger = logging.getLogger("amc.active_role")

ACTIVE_WINDOW_DAYS = 30


async def get_active_discord_ids(window_days: int = ACTIVE_WINDOW_DAYS) -> set[int]:
    """Discord user IDs of linked players with a login inside the window."""
    cutoff = timezone.now() - timedelta(days=window_days)
    rows = [
        row
        async for row in Player.objects.filter(
            discord_user_id__isnull=False,
            characters__status_logs__timespan__startswith__gte=cutoff,
        )
        .values_list("discord_user_id", flat=True)
        .distinct()
    ]
    return set(rows)


def compute_role_changes(
    active_ids: set[int], member_ids_with_role: set[int]
) -> tuple[list[int], list[int]]:
    """Deterministic (to_add, to_remove) lists, sorted for stable logs/tests."""
    return sorted(active_ids - member_ids_with_role), sorted(
        member_ids_with_role - active_ids
    )
