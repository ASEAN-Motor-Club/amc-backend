"""Shared helpers for the admin force-RP (RP-mode lock) feature.

Mirrors the forced-name lock: an admin locks a player into RP mode for a
bounded duration, the player cannot toggle it off with /rp_mode until the
expiry timestamp passes, and the lock is account-level so switching
characters can't escape it.

Kept in a dedicated module (not under commands/) so both the in-game command
handlers and the Discord cog can share the audit-trail writer and the
apply/clear logic without an import cycle.
"""

from datetime import datetime, timedelta

from django.utils import timezone

from amc.models import Character, ForcedRPLog, Player


def is_forced_rp(player: Player) -> datetime | None:
    """Return the forced-RP expiry timestamp if the player is currently locked.

    None means not forced (either never locked, or the lock has naturally
    expired — computed, so no cron is needed to lift it).
    """
    until = getattr(player, "forced_rp_until", None)
    if until is None:
        return None
    return until if until > timezone.now() else None


async def log_forced_rp_change(
    player: Player,
    *,
    action: str,
    old_until: datetime | None,
    new_until: datetime | None,
    actor_character: Character | None = None,
    actor_player: Player | None = None,
    actor_discord_id: int | None = None,
):
    """Append a row to the forced-RP audit log.

    Exactly one actor signature should be provided: either an in-game
    character (+ player account) or a Discord user id.
    """
    await ForcedRPLog.objects.acreate(
        player=player,
        action=action,
        old_until=old_until,
        new_until=new_until,
        actor_character=actor_character,
        actor_player=actor_player,
        actor_discord_id=actor_discord_id,
    )


async def enforce_forced_rp_on_login(character, player):
    """Ensure a character is in RP mode if their account is currently locked.

    Called on login (before refresh_player_name) so a forced player can't
    escape the lock by logging out and back in. Returns True if the character
    was (re)locked into RP mode by this call.
    """
    if not character or is_forced_rp(player) is None:
        return False
    from amc.models import RPSession

    if not character.rp_mode:
        await RPSession.objects.aget_or_create(character=character, expires_at=None)
        character.rp_mode = True
        await character.asave(update_fields=["rp_mode"])
        return True
    return False


async def apply_forced_rp(
    player: Player,
    *,
    hours: float,
    http_client_mod=None,
    actor_character: Character | None = None,
    actor_player: Player | None = None,
    actor_discord_id: int | None = None,
) -> timedelta:
    """Lock a player into RP mode for `hours`.

    Sets Player.forced_rp_until, records the audit row, and — for the player's
    currently online character — turns RP mode on and re-pushes the name so the
    [R] tag is visible immediately (offline characters are covered on login by
    the login enforcement in tasks.py).

    Returns the lock duration as a timedelta (for the admin reply).
    """
    from amc.player_tags import refresh_player_name

    old_until = player.forced_rp_until
    until = timezone.now() + timedelta(hours=hours)
    player.forced_rp_until = until
    await player.asave(update_fields=["forced_rp_until"])

    await log_forced_rp_change(
        player,
        action="set",
        old_until=old_until,
        new_until=until,
        actor_character=actor_character,
        actor_player=actor_player,
        actor_discord_id=actor_discord_id,
    )

    # Turn RP mode on for the online character (if any) and push the [R] tag
    # now; offline characters get forced on at next login.
    from amc.models import RPSession

    online_chars = Character.objects.filter(player=player).exclude(guid=None).exclude(
        guid=""
    )
    async for character in online_chars:
        if not character.rp_mode:
            await RPSession.objects.aget_or_create(
                character=character, expires_at=None
            )
            character.rp_mode = True
            await character.asave(update_fields=["rp_mode"])
        # refresh_player_name reads character.rp_mode → recomputes the [R] tag.
        await refresh_player_name(character, http_client_mod)

    return timedelta(hours=hours)


async def clear_forced_rp(
    player: Player,
    *,
    http_client_mod=None,
    actor_character: Character | None = None,
    actor_player: Player | None = None,
    actor_discord_id: int | None = None,
) -> bool:
    """Release a player's forced-RP lock early.

    Returns True if a lock existed and was cleared. Does NOT turn off an
    rp_mode the player enabled on their own — it only removes the lock so they
    can toggle RP mode off normally.
    """
    old_until = player.forced_rp_until
    if old_until is None:
        return False

    player.forced_rp_until = None
    await player.asave(update_fields=["forced_rp_until"])

    await log_forced_rp_change(
        player,
        action="clear",
        old_until=old_until,
        new_until=None,
        actor_character=actor_character,
        actor_player=actor_player,
        actor_discord_id=actor_discord_id,
    )

    return True
