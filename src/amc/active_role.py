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

import discord
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


_SYNCED_REASON_ADD = "Active role: logged in within 30 days"
_SYNCED_REASON_REMOVE = "Active role: inactive 30+ days"


async def sync_active_role(bot) -> dict:
    """Sync the Active role against the DB-derived active set.

    Must run on the bot's event loop (discord.py objects are not
    thread-safe). Returns a summary dict with skipped/added/removed/missing.
    """
    empty = {"skipped": True, "added": 0, "removed": 0, "missing": 0}
    role_id = int(getattr(settings, "DISCORD_ACTIVE_ROLE_ID", 0) or 0)
    if not role_id:
        logger.info("Active role sync: DISCORD_ACTIVE_ROLE_ID not set — skipped")
        return empty

    guild = bot.get_guild(settings.DISCORD_GUILD_ID)
    if guild is None:
        logger.warning(
            "Active role sync: guild %s not found", settings.DISCORD_GUILD_ID
        )
        return empty
    role = guild.get_role(role_id)
    if role is None:
        logger.warning(
            "Active role sync: role %s not found in guild %s",
            role_id,
            settings.DISCORD_GUILD_ID,
        )
        return empty

    active_ids = await get_active_discord_ids()
    to_add, to_remove = compute_role_changes(active_ids, {m.id for m in role.members})

    added = removed = missing = 0
    for uid in to_add:
        member = guild.get_member(uid)
        if member is None:
            # Not in the member cache (joined recently, or left the guild).
            # One API fetch as fallback before giving up for this run.
            try:
                member = await guild.fetch_member(uid)
            except discord.NotFound:
                missing += 1
                continue
            except discord.HTTPException:
                logger.exception("Active role sync: fetch_member(%s) failed", uid)
                continue
        try:
            await member.add_roles(role, reason=_SYNCED_REASON_ADD)
            added += 1
        except discord.Forbidden:
            logger.error(
                "Active role sync: forbidden adding role to %s — check the "
                "bot's role sits above the Active role",
                uid,
            )
        except discord.HTTPException:
            logger.exception("Active role sync: failed to add role to %s", uid)

    for uid in to_remove:
        member = guild.get_member(uid)
        if member is None:
            # role.members comes from the same cache; should not happen
            continue
        try:
            await member.remove_roles(role, reason=_SYNCED_REASON_REMOVE)
            removed += 1
        except discord.Forbidden:
            # Includes the guild owner: Discord forbids role edits on the
            # owner regardless of the bot's permissions.
            logger.error(
                "Active role sync: forbidden removing role from %s — check "
                "role hierarchy (guild owners can never be modified)",
                uid,
            )
        except discord.HTTPException:
            logger.exception("Active role sync: failed to remove role from %s", uid)

    summary = {
        "skipped": False,
        "added": added,
        "removed": removed,
        "missing": missing,
    }
    logger.info("Active role sync: %s", summary)
    return summary


async def active_role_cron(ctx):
    """arq cron entry. The bot runs in a thread with its own loop — marshal
    the coroutine over (same cross-thread pattern as amc/tasks.py)."""
    client = ctx.get("discord_client")
    if client is None or not client.is_ready():
        logger.warning("Active role sync skipped: Discord client not ready")
        return
    fut = asyncio.run_coroutine_threadsafe(sync_active_role(client), client.loop)
    summary = await asyncio.wrap_future(fut)
    logger.info("Active role cron done: %s", summary)
