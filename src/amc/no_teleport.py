"""Backend-pushed invisible no-teleport flag (freeman 2026-09-23).

The mod (MTDediMod NoTeleportManager) keeps an in-memory per-GUID set and
blocks ServerTeleportCharacter / ServerTeleportVehicle / ServerRespawnCharacter
for flagged GUIDs — no display-name involvement, so the flag reveals nothing
about wanted status (unlike the [R] tag).

The backend is the source of truth:
- Manual flags persist on ``Character.no_teleport`` (admin /noteleport) and are
  re-asserted on every player login (the mod's set is memory-only and a game
  restart clears it).
- The wanted-grace window pushes the flag transiently while a PendingWanted
  exists, and clears it at apply/drop.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def push_no_teleport(character, http_client_mod, enabled: bool) -> None:
    """Push the no-teleport flag for one character to the mod. Best-effort."""
    if not http_client_mod or not character.guid:
        return
    try:
        from amc import mod_server

        await mod_server.set_no_teleport(http_client_mod, character.guid, enabled)
        logger.info(
            "no-teleport flag %s for %s (%s)",
            "ENABLED" if enabled else "cleared",
            character.name,
            character.guid,
        )
    except Exception as e:  # noqa: BLE001 — push must never break the caller
        logger.warning(
            "Failed to push no-teleport %s for %s: %s",
            "ENABLE" if enabled else "CLEAR",
            character.name,
            e,
        )


def push_no_teleport_later(character, http_client_mod, enabled: bool) -> None:
    """Fire-and-forget variant for event-handler contexts."""
    asyncio.create_task(push_no_teleport(character, http_client_mod, enabled))
