"""Backend-pushed invisible no-teleport flag (freeman 2026-09-23).

The mod (MTDediMod NoTeleportManager) keeps an in-memory per-GUID map of
GUID -> lock MODE and blocks movement RPCs for flagged GUIDs — no display-name
involvement, so the flag reveals nothing about wanted status (unlike the
[R] tag).

Lock MODES (freeman 2026-09-27: "allow different types of teleport blocking"):
- ``MODE_ALL`` — block every movement RPC. Used for wanted / wanted-grace /
  manual admin holds, and for wanted suspects while a cop is within the
  roadside gate distance (no roadside allowance while the chase is close).
- ``MODE_WANTED_ROADSIDE`` — same full teleport lock as ``MODE_ALL`` EXCEPT
  ``ServerResetVehicleAt``: the cargo-kept roadside flow (bRemoveCargo=false)
  passes, the cargo-strip flow (bRemoveCargo=true) is pinned. Used for wanted
  suspects while every on-duty cop is beyond the 500 m gate (freeman
  2026-09-28: "cops > 500m = roadside reset teleport allowed").
- ``MODE_RESET_CARGO_KEEP`` — block ONLY ``ServerResetVehicleAt`` with
  ``bRemoveCargo=false`` (the cargo-kept roadside flow that teleports the
  vehicle with its cargo); ``bRemoveCargo=true`` passes and the other three
  movement RPCs are unaffected. Used for on-duty police while they are within
  500 m of an active wanted (freeman 2026-09-28: police may roadside-reset
  with cargo-strip always, cargo-kept only when >500 m from wanteds).

The backend is the source of truth:
- Manual flags persist on ``Character.no_teleport`` (admin /noteleport) and are
  re-asserted on every player login (the mod's map is memory-only and a game
  restart clears it).
- The wanted-grace window pushes the flag transiently while a PendingWanted
  exists, and clears it at apply/drop.
- The wanted-tick refines the mode by police distance every tick
  (``push_no_teleport_cached`` — transition-only pushes); every
  ``sync_no_teleport`` invalidates the tick's cache entry so a foreign push
  (login, /noteleport, duty change, wanted create/clear) is re-asserted
  correctly on the next tick.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

MODE_ALL = "all"
MODE_WANTED_ROADSIDE = "wanted_roadside"
MODE_RESET_CARGO_KEEP = "reset_cargo_keep"

# Last (enabled, mode) state the wanted-tick pushed per guid. The tick only
# pushes on transitions; sync_no_teleport pops the entry so the next tick
# re-pushes its computed state after any foreign push.
_pushed_lock_state: dict[str, tuple[bool, str]] = {}


async def teleport_lock_mode(character) -> str | None:
    """Effective lock MODE for one character, or None when unlocked.

    Priority: manual flag / wanted / wanted-grace (PendingWanted) all yield
    MODE_ALL. On-duty police yield MODE_RESET_CARGO_KEEP (cargo-strip resets
    always allowed; cargo-kept roadside locked pending the wanted-tick's
    distance refinement, which clears or keeps it per the 500 m gate).
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
        # Police get the narrow lock (freeman 2026-09-28 PR2 spec): the
        # cargo-strip ServerResetVehicleAt always passes, the cargo-kept
        # roadside flow is locked until the wanted-tick's distance gate
        # clears it (>500 m from every active wanted). Requires the
        # wanted_roadside mod rc for full enforcement; older mods either
        # ignore the narrow mode's TP effect or reject unknown modes with
        # the previous flag state left intact (best-effort pushes).
        return MODE_RESET_CARGO_KEEP
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


async def push_no_teleport_cached(
    character, http_client_mod, enabled: bool, mode: str = MODE_ALL
) -> None:
    """Transition-only push for the wanted-tick's per-second refinement.

    Skips the HTTP call when the (enabled, mode) pair for this guid was
    already pushed by a previous tick. ``sync_no_teleport`` pops the cache
    entry, so any foreign push (login re-assert, /noteleport, duty change,
    wanted create/clear) is followed by a fresh tick push of whatever the
    distance gate computes.
    """
    prev = _pushed_lock_state.get(character.guid)
    if prev == (enabled, mode):
        return
    await push_no_teleport(character, http_client_mod, enabled, mode)
    if http_client_mod and character.guid:
        _pushed_lock_state[character.guid] = (enabled, mode)


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
    # Invalidate the wanted-tick's transition cache: whatever we push here
    # (login, duty change, wanted create/clear, /noteleport), the next tick
    # must re-push its own distance-gated state instead of trusting a stale
    # "already pushed" entry.
    _pushed_lock_state.pop(character.guid, None)
    await push_no_teleport(character, http_client_mod, mode is not None, mode or MODE_ALL)
