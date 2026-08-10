"""Shared helpers for the admin force-rename (name-lock) feature.

Kept in a dedicated module (not under commands/) so both the in-game command
handlers and the Discord cog can share the audit-trail writer without an
import cycle.
"""

from typing import Optional

from amc.models import ForcedNameLog, Player, Character


async def log_forced_name_change(
    player: Player,
    *,
    action: str,
    old_name: Optional[str],
    new_name: Optional[str],
    actor_character: Optional[Character] = None,
    actor_player: Optional[Player] = None,
    actor_discord_id: Optional[int] = None,
):
    """Append a row to the forced-name audit log.

    Exactly one actor signature should be provided: either an in-game
    character (+ player account) or a Discord user id.
    """
    await ForcedNameLog.objects.acreate(
        player=player,
        action=action,
        old_name=old_name or None,
        new_name=new_name or None,
        actor_character=actor_character,
        actor_player=actor_player,
        actor_discord_id=actor_discord_id,
    )