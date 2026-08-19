"""Orchestrator for login-time offensive-name auto-moderation.

Wire-in: on `PlayerLoginLogEvent`, the worker schedules
`run_name_moderation(character, player, http_client, http_client_mod)` as a
non-blocking task. This ties together the OpenRouter LLM judge (which replaces
the old deterministic regex blocklist — removed because the game already enforces
offensive names at the source), performs the account-level `forced_name` lock,
applies it live, records audit rows, and (per settings) announces neutrally and
posts manual-review items to Discord with Rename/Whitelist action buttons.

Every failure degrades to a logged no-action — this never blocks a login and
never raises into the login path.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

from amc.llm_judge import judge_name
from amc.models import NameModerationLog, NameWhitelist, Player
from amc.name_moderation import strip_reserved_tags

logger = logging.getLogger(__name__)

_RESERVED_PREFIXES = ("[RED", "[GOV", "[D]", "[MOD]", "[MODS]", "[DOT]",
                      "[EVENT]", "[ADMIN]", "[P]", "[PN]", "[C]")
_MAX_NAME_LEN = 30

# Only racist-related violations may auto-rename (at NAMER_AUTO_CONFIDENCE_THRESHOLD).
# Every other category routes to manual review regardless of confidence.
_AUTO_RENAME_CATEGORIES = frozenset({"racial_slur"})


def _cfg(name: str, default):
    return getattr(settings, name, default)


def _is_reserved_display(name: str) -> bool:
    """Reserved/`staff` tag prefixes must never be auto-renamed."""
    return (name or "").upper().startswith(_RESERVED_PREFIXES)


def _safe_suggested_name(suggested: str | None) -> str | None:
    """Validate the LLM-proposed replacement before it can be written."""
    if not suggested:
        return None
    s = suggested.strip()
    if not s or len(s) > _MAX_NAME_LEN:
        return None
    if _is_reserved_display(s):
        return None
    if not all(c.isalnum() or c in " _-." for c in s):
        return None
    return s


async def _record(
    character, player, base_name, *, source, is_violation, confidence,
    action, categories=None, suggested_name=None, reason=None,
) -> NameModerationLog:
    return await NameModerationLog.objects.acreate(
        player=player,
        character=character,
        base_name=base_name,
        verdict_source=source,
        is_violation=is_violation,
        confidence=float(confidence or 0.0),
        categories=list(categories or []),
        action=action,
        suggested_name=suggested_name,
        llm_model=_cfg("NAMER_LLM_MODEL", "") if source == "llm" else "",
        reason=reason or "",
    )


async def _apply_name_lock(
    player, character, target, http_client, http_client_mod, *,
    actor_discord_id=None,
) -> str:
    """Set forced_name + audit + live refresh + optional announce.

    Shared by the login auto-rename path and the manual-review Rename button so
    both produce identical name-lock behaviour.
    """
    from amc.forced_name import log_forced_name_change
    from amc.player_tags import refresh_player_name

    if character is not None:
        old = character.custom_name or character.name
    else:
        old = player.forced_name or ""
    clean_target = _safe_suggested_name(target) or _cfg("NAMER_CANNED_FALLBACK_NAME",
                                                        "FriendlyPlayer")
    await player.__class__.objects.filter(
        unique_id=player.unique_id
    ).aupdate(forced_name=clean_target)
    await log_forced_name_change(
        player, action="set", old_name=old, new_name=clean_target,
        actor_discord_id=actor_discord_id,
    )
    logger.info("apply name lock %r -> %r (uid=%s, actor_discord=%s)",
                old, clean_target, player.unique_id, actor_discord_id)
    if character is not None:
        try:
            # Re-runs the name chokepoint which honours forced_name async-safely.
            await refresh_player_name(character, http_client_mod)
        except Exception:
            logger.exception("refresh_player_name failed after renaming %r", old)

    if _cfg("NAMER_ANNOUNCE", True):
        try:
            from amc.game_server import announce

            msg = (
                "The display name was changed to follow our community rules. "
                f"The player now shows as {clean_target}."
            )
            await announce(msg, http_client)
        except Exception:
            logger.exception("in-game announce failed after name lock")

    return clean_target


async def _apply_name_unlock(
    player, character, http_client_mod, *, actor_discord_id=None,
) -> None:
    """Clear an account-level forced-name lock + audit + live refresh.

    Mirrors `cmd_clear_forced_name` (restore the player their own name again)
    so the auto-rename Undo button returns a false-positive victim to full
    naming authority instead of permanently locking them to the original name.
    """
    from amc.forced_name import log_forced_name_change
    from amc.player_tags import refresh_player_name

    old = player.forced_name
    await player.__class__.objects.filter(
        unique_id=player.unique_id
    ).aupdate(forced_name=None)
    await log_forced_name_change(
        player, action="clear", old_name=old, new_name=None,
        actor_discord_id=actor_discord_id,
    )
    logger.info("clear name lock %r (uid=%s, actor_discord=%s)",
                old, player.unique_id, actor_discord_id)
    if character is not None:
        try:
            await refresh_player_name(character, http_client_mod)
        except Exception:
            logger.exception("refresh_player_name failed after clearing lock %r", old)


async def _apply_rename(
    character, player, base_name, *, to, source, confidence,
    categories, http_client, http_client_mod, reason=None,
):
    """Account-level lock + live push + audit + optional announce (auto path)."""
    clean_target = await _apply_name_lock(
        player, character, to, http_client, http_client_mod,
    )
    log = await _record(
        character, player, base_name, source=source, is_violation=True,
        confidence=confidence, action="rename", categories=categories,
        suggested_name=clean_target, reason=reason,
    )
    await _post_rename_to_discord(
        log.pk, base_name, clean_target, source=source,
        confidence=confidence, categories=categories, reason=reason,
    )


async def _post_rename_to_discord(
    log_id, base_name, clean_target, *, source, confidence, categories, reason,
):
    """Log a completed auto-rename to the review channel WITH an Undo button.

    Uses `enqueue_discord_rename_audit` (not `enqueue_discord_message`) so the
    audit post carries an admin-gated "Undo & Whitelist" button — a false-positive
    rename like N17R0 -> NITRO can be reverted straight from the message.
    """
    channel_id = _cfg("NAMER_REVIEW_CHANNEL_ID", None)
    if not channel_id:
        return
    try:
        from amc.tasks import enqueue_discord_rename_audit

        reason_txt = (reason or "").strip()
        msg = (
            f"Auto-renamed `{base_name}` → `{clean_target}` "
            f"(source={source}, conf={confidence:.2f}, "
            f"cats={categories or []})"
        )
        if reason_txt:
            msg += f". Reason: {reason_txt}"
        enqueue_discord_rename_audit(str(channel_id), log_id, msg, timezone.now())
    except Exception:
        logger.exception("Discord rename-log enqueue failed")


async def _log_manual_review(character, player, base_name, verdict):
    log = await _record(
        character, player, base_name, source="llm", is_violation=True,
        confidence=verdict.confidence, action="manual_review",
        categories=verdict.categories, suggested_name=verdict.suggested_name,
        reason=verdict.reason,
    )
    channel_id = _cfg("NAMER_REVIEW_CHANNEL_ID", None)
    if channel_id:
        try:
            from amc.tasks import enqueue_discord_review

            review = (
                f"Name needs manual review: `{base_name}` "
                f"(conf={verdict.confidence:.2f}, cats={verdict.categories}). "
                f"LLM suggested: {verdict.suggested_name or 'none'}."
            )
            enqueue_discord_review(str(channel_id), log.pk, review, timezone.now())
        except Exception:
            logger.exception("Discord review enqueue failed")


async def apply_review_rename(
    log_id, *, actor_discord_id, http_client, http_client_mod,
) -> str:
    """Rename a player from the manual-review Rename button.

    Looks up the `NameModerationLog` row, applies the forced-name lock through the
    shared `_apply_name_lock`, flips the audit action to `rename`, and returns the
    applied name.
    """
    log = await NameModerationLog.objects.select_related("character").aget(pk=log_id)
    player = await Player.objects.aget(unique_id=log.player_id)
    character = log.character
    target = log.suggested_name or _cfg("NAMER_CANNED_FALLBACK_NAME",
                                        "FriendlyPlayer")
    new_name = await _apply_name_lock(
        player, character, target, http_client, http_client_mod,
        actor_discord_id=actor_discord_id,
    )
    log.action = NameModerationLog.Action.RENAME
    log.suggested_name = new_name
    await log.asave(update_fields=["action", "suggested_name"])
    return new_name


async def apply_review_whitelist(log_id, *, actor_discord_id) -> str:
    """Whitelist a name for its owning player from the review Whitelist button.

    Persists a `NameWhitelist` row (per player) so future logins skip the LLM for
    that name, and flips the audit action to `whitelist`.
    """
    log = await NameModerationLog.objects.aget(pk=log_id)
    base = strip_reserved_tags(log.base_name or "").strip().lower()
    if base:
        await NameWhitelist.objects.aget_or_create(
            player_id=log.player_id,
            name=base,
            defaults={"added_by": actor_discord_id,
                      "reason": "approved via manual review"},
        )
    log.action = NameModerationLog.Action.WHITELIST
    await log.asave(update_fields=["action"])
    return base


async def apply_auto_rename_undo(
    log_id, *, actor_discord_id, http_client, http_client_mod,
) -> str:
    """Undo an auto-rename AND whitelist the original name (audit-channel button).

    An auto-rename logged against an innocent name (LLM false positive) is
    reverted: the account-level forced-name lock is reset to the ORIGINAL
    base_name, the name is per-player whitelisted so the LLM judge is skipped
    on future logins (without this it would just be re-renamed), and the audit
    row's action is flipped to `undone`. Only valid for `rename` logs.
    """
    log = await NameModerationLog.objects.select_related("character").aget(
        pk=log_id
    )
    if log.action != NameModerationLog.Action.RENAME:
        raise ValueError(
            f"log {log_id} is not an auto-rename (action={log.action})"
        )
    base = strip_reserved_tags(log.base_name or "").strip()
    if not base:
        raise ValueError(f"log {log_id} has no base_name to restore")
    player = await Player.objects.aget(unique_id=log.player_id)
    # Restore the player's own name by CLEARING the forced lock (mirrors
    # cmd_clear_forced_name) so they regain full naming authority — not by
    # re-locking them to the original name for good. Whitelisting below keeps
    # the LLM judge from re-flagging the innocent name on their next login.
    await _apply_name_unlock(
        player, log.character, http_client_mod,
        actor_discord_id=actor_discord_id,
    )
    await NameWhitelist.objects.aget_or_create(
        player_id=log.player_id,
        name=base.lower(),
        defaults={"added_by": actor_discord_id,
                  "reason": "undone auto-rename (admin approved)"},
    )
    log.action = NameModerationLog.Action.UNDONE
    log.suggested_name = base
    await log.asave(update_fields=["action", "suggested_name"])
    return base


async def _is_whitelisted(player, base: str) -> bool:
    return await NameWhitelist.objects.filter(
        player=player, name=base.strip().lower()
    ).aexists()


async def run_name_moderation(
    character, player, http_client, http_client_mod,
):
    """Moderate a player's display name on login (non-blocking, never raises)."""
    if not _cfg("NAMER_ENABLED", False):
        return
    try:
        display = character.custom_name or character.name
        base = strip_reserved_tags(display).strip()
        if not base or _is_reserved_display(display):
            return

        # Per-player whitelist short-circuit — approved names skip the LLM.
        if await _is_whitelisted(player, base):
            await _record(
                character, player, base, source="whitelist", is_violation=False,
                confidence=1.0, action="none", reason="admin_whitelisted",
            )
            return

        # Stage B — LLM structured verdict (cache-guarded).
        verdict, source = await judge_name(base)
        if source == "error":
            await _record(character, player, base, source="error",
                          is_violation=False, confidence=0.0, action="none",
                          reason="judge_error")
            return

        if (
            verdict.is_violation
            and verdict.confidence >= _cfg("NAMER_AUTO_CONFIDENCE_THRESHOLD", 0.9)
            and verdict.recommended_action == "rename"
            and (_AUTO_RENAME_CATEGORIES & set(verdict.categories or []))
        ):
            to = _safe_suggested_name(verdict.suggested_name) or _cfg(
                "NAMER_CANNED_FALLBACK_NAME", "FriendlyPlayer"
            )
            await _apply_rename(
                character, player, base, to=to, source=source,
                confidence=verdict.confidence, categories=verdict.categories,
                http_client=http_client, http_client_mod=http_client_mod,
                reason=verdict.reason,
            )
            return

        if verdict.is_violation:
            await _log_manual_review(character, player, base, verdict)
            return

        await _record(
            character, player, base, source=source, is_violation=False,
            confidence=verdict.confidence, action="none",
            categories=verdict.categories, reason=verdict.reason,
        )
    except Exception:
        logger.exception("run_name_moderation failed for uid=%s",
                         getattr(player, "unique_id", None))
