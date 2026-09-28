"""Backend-pushed invisible no-teleport flag (freeman 2026-09-23).

The mod (MTDediMod NoTeleportManager) keeps an in-memory per-GUID map of
GUID -> lock MODE and blocks movement RPCs for flagged GUIDs — no display-name
involvement, so the flag reveals nothing about wanted status (unlike the
[R] tag).

Lock MODES (freeman 2026-09-27: "allow different types of teleport blocking"):
- ``MODE_ALL`` — block every movement RPC. Used for wanted / wanted-grace /
  manual admin holds.
- ``MODE_RESET_CARGO_KEEP`` — block ONLY ``ServerResetVehicleAt`` with
  ``bRemoveCargo=false`` (the cargo-kept roadside flow that teleports the
  vehicle with its cargo); ``bRemoveCargo=true`` passes and the other three
  movement RPCs are unaffected. Used for on-duty police.

The backend is the source of truth:
- Manual flags persist on ``Character.no_teleport`` (admin /noteleport) and are
  re-asserted on every player login (the mod's map is memory-only and a game
  restart clears it).
- The wanted-grace window pushes the flag transiently while a PendingWanted
  exists, and clears it at apply/drop.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

MODE_ALL = "all"
MODE_RESET_CARGO_KEEP = "reset_cargo_keep"


async def teleport_lock_mode(character) -> str | None:
    """Effective lock MODE for one character, or None when unlocked.

    Priority: manual flag / wanted / wanted-grace (PendingWanted) all yield
    MODE_ALL. On-duty police yields MODE_RESET_CARGO_KEEP ONLY while a live
    wanted criminal exists on the server (freeman 2026-09-27: outside active
    chases there is nothing to enforce, so police are fully unlocked) —
    police stay able to use cargo-strip resets; only the cargo-kept roadside
    flow is blocked. Any ALL-source wins over police.
    """
    from amc.models import PendingWanted, PoliceSession, Wanted

    if character.no_teleport:
        return MODE_ALL
    if await Wanted.objects.filter(
        character=character,
        expired_at__isnull=True,
        wanted_remaining__gt=0,
    ).aexists():
        return MODE_ALL
    if await PendingWanted.objects.filter(character=character).aexists():
        return MODE_ALL
    if await PoliceSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aexists():
        if await Wanted.objects.filter(
            expired_at__isnull=True, wanted_remaining__gt=0
        ).aexists():
            return MODE_RESET_CARGO_KEEP
        return None
    return None


async def push_no_teleport(
    character, http_client_mod, enabled: bool, mode: str = MODE_ALL
) -> None:
    """Push the no-teleport flag (+ lock mode) for one character. Best-effort."""
    if not http_client_mod or not character.guid:
        return
    try:
        from amc import mod_server

        await mod_server.set_no_teleport(http_client_mod, character.guid, enabled, mode)
        logger.info(
            "no-teleport flag %s (mode=%s) for %s (%s)",
            "ENABLED" if enabled else "cleared",
            mode if enabled else "-",
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


def push_no_teleport_later(
    character, http_client_mod, enabled: bool, mode: str = MODE_ALL
) -> None:
    """Fire-and-forget variant for event-handler contexts."""
    asyncio.create_task(push_no_teleport(character, http_client_mod, enabled, mode))


async def is_teleport_locked(character) -> bool:
    """DB-truth teleport lock: manual flag OR on-duty police OR wanted OR
    wanted-grace (PendingWanted).

    The mod-side push is only the in-game enforcement carrier; this is the
    authoritative check for backend decisions (login re-assert, command
    gates). PendingWanted counts so a logout/login inside the 30s grace
    window keeps the lock: the re-assert used to compute effective from the
    Wanted table only and silently unlock a graced player.
    """
    return await teleport_lock_mode(character) is not None


async def sync_no_teleport(character, http_client_mod) -> None:
    """Push the EFFECTIVE flag (+ mode) for one character.

    Replaces the [R] name tag as the teleport-lock carrier (freeman
    2026-09-23): on-duty police and wanted suspects are flagged invisibly.
    Authoritative — computes from current DB state and pushes the result, so
    it is safe to call at any transition point (login, activate/deactivate,
    wanted create/expire/arrest-clear, pending-wanted apply/drop).
    """
    mode = await teleport_lock_mode(character)
    await push_no_teleport(character, http_client_mod, mode is not None, mode or MODE_ALL)
