"""Backend-pushed invisible no-teleport BLOCK SET (freeman 2026-09-23).

The mod (MTDediMod NoTeleportManager) keeps an in-memory per-GUID block set
and blocks movement RPCs for flagged GUIDs — no display-name involvement, so
the flag reveals nothing about wanted status (unlike the [R] tag).

Domain-agnostic block-set schema (freeman 2026-09-28: "no teleport should be
agnostic — accept flags for each type of teleport, whether they should be
allowed or not"): each teleport TYPE is an independent block flag; the mod
holds no wanted/police/mode vocabulary — the backend composes the set.

Block flags (absent guid = unrestricted; absent key = allowed):
- ``block_teleport_character`` — ServerTeleportCharacter
- ``block_teleport_vehicle`` — ServerTeleportVehicle
- ``block_respawn_character`` — ServerRespawnCharacter
- ``block_reset_vehicle_keep_cargo`` — ServerResetVehicleAt with
  ``bRemoveCargo=false`` (roadside flow that moves the vehicle WITH cargo)
- ``block_reset_vehicle_strip_cargo`` — ServerResetVehicleAt with
  ``bRemoveCargo=true`` (cargo-strip reset)

The backend is the source of truth:
- Manual flags persist on ``Character.no_teleport`` (admin /noteleport) and are
  re-asserted on every player login (the mod's map is memory-only and a game
  restart clears it).
- The wanted-grace window pushes the full set transiently while a PendingWanted
  exists, and clears it at apply/drop.
- The wanted-tick refines the set by police distance every tick
  (``push_no_teleport_cached`` — transition-only pushes); every
  ``sync_no_teleport`` invalidates the tick's cache entry so a foreign push
  (login, /noteleport, duty change, wanted create/clear) is re-asserted
  correctly on the next tick.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

BLOCK_TELEPORT_CHARACTER = "block_teleport_character"
BLOCK_TELEPORT_VEHICLE = "block_teleport_vehicle"
BLOCK_RESPAWN_CHARACTER = "block_respawn_character"
BLOCK_RESET_VEHICLE_KEEP_CARGO = "block_reset_vehicle_keep_cargo"
BLOCK_RESET_VEHICLE_STRIP_CARGO = "block_reset_vehicle_strip_cargo"

# Full lock: every movement RPC blocked (manual admin hold / graced player /
# wanted suspect while a cop is within the 500 m roadside gate).
FULL_BLOCKS: dict[str, bool] = {
    BLOCK_TELEPORT_CHARACTER: True,
    BLOCK_TELEPORT_VEHICLE: True,
    BLOCK_RESPAWN_CHARACTER: True,
    BLOCK_RESET_VEHICLE_KEEP_CARGO: True,
    BLOCK_RESET_VEHICLE_STRIP_CARGO: True,
}

# Wanted suspect while every on-duty cop is beyond the 500 m gate (freeman
# 2026-09-28: "cops > 500m = roadside reset teleport allowed"): full lock
# except the cargo-kept roadside reset. ServerVehicleExControl (roadside
# service) is NOT name-gated here — it doesn't work per freeman 2026-10-03;
# roadside enforcement is entirely the ServerResetVehicleAt block keys.
WANTED_ROADSIDE_BLOCKS: dict[str, bool] = {
    BLOCK_TELEPORT_CHARACTER: True,
    BLOCK_TELEPORT_VEHICLE: True,
    BLOCK_RESPAWN_CHARACTER: True,
    BLOCK_RESET_VEHICLE_STRIP_CARGO: True,
}

# On-duty police near an active wanted: only the cargo-kept roadside reset is
# blocked — the cargo-strip reset always passes for police.
POLICE_NEAR_BLOCKS: dict[str, bool] = {
    BLOCK_RESET_VEHICLE_KEEP_CARGO: True,
}

# Last block-set state the wanted-tick pushed per guid (as a hashable tuple).
# The tick only pushes on transitions; sync_no_teleport pops the entry so the
# next tick re-pushes its computed state after any foreign push.
_pushed_lock_state: dict[str, tuple | None] = {}


def _blocks_key(blocks: dict | None) -> tuple | None:
    if not blocks:
        return None
    return tuple(sorted(blocks.items()))


async def teleport_lock_blocks(character) -> dict | None:
    """Effective no-teleport block set for one character, or None.

    Priority: manual flag / wanted / wanted-grace (PendingWanted) all yield
    the full set. On-duty police yield the narrow set (cargo-strip resets
    always allowed; cargo-kept roadside locked pending the wanted-tick's
    distance refinement, which clears it beyond the 500 m gate).
    """
    from amc.models import PendingWanted, PoliceSession, Wanted

    if character.no_teleport:
        return dict(FULL_BLOCKS)
    if await Wanted.objects.filter(
        character=character,
        expired_at__isnull=True,
        wanted_remaining__gt=0,
    ).aexists():
        return dict(FULL_BLOCKS)
    if await PendingWanted.objects.filter(character=character).aexists():
        return dict(FULL_BLOCKS)
    if await PoliceSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aexists():
        # Police get the narrow set (freeman 2026-09-28 PR2 spec): the
        # cargo-strip ServerResetVehicleAt always passes, the cargo-kept
        # roadside flow is locked until the wanted-tick's distance gate
        # clears it (>500 m from every active wanted).
        return dict(POLICE_NEAR_BLOCKS)
    return None


async def push_no_teleport(character, http_client_mod, blocks: dict | None = None) -> None:
    """Push the no-teleport block set for one character. Best-effort.

    ``None``/empty ``blocks`` clears the record (DELETE); otherwise POSTs
    ``{"Blocks": blocks}``.
    """
    if not http_client_mod or not character.guid:
        return
    try:
        from amc import mod_server

        await mod_server.set_no_teleport(http_client_mod, character.guid, blocks)
        logger.info(
            "no-teleport flags %s for %s (%s)",
            sorted(blocks) if blocks else "cleared",
            character.name,
            character.guid,
        )
    except Exception as e:  # noqa: BLE001 — push must never break the caller
        logger.warning(
            "Failed to push no-teleport %s for %s: %s",
            "set" if blocks else "clear",
            character.name,
            e,
        )


def push_no_teleport_later(
    character, http_client_mod, blocks: dict | None = None
) -> None:
    """Fire-and-forget variant for event-handler contexts."""
    asyncio.create_task(push_no_teleport(character, http_client_mod, blocks))


async def push_no_teleport_cached(
    character, http_client_mod, blocks: dict | None = None
) -> None:
    """Transition-only push for the wanted-tick's per-second refinement.

    Skips the HTTP call when the block set for this guid was already pushed
    by a previous tick. ``sync_no_teleport`` pops the cache entry, so any
    foreign push (login re-assert, /noteleport, duty change, wanted
    create/clear) is followed by a fresh tick push of whatever the distance
    gate computes.
    """
    key = _blocks_key(blocks)
    if _pushed_lock_state.get(character.guid, "MISSING") == key:
        return
    await push_no_teleport(character, http_client_mod, blocks)
    if http_client_mod and character.guid:
        _pushed_lock_state[character.guid] = key


async def is_teleport_locked(character) -> bool:
    """DB-truth teleport lock: manual flag OR on-duty police OR wanted OR
    wanted-grace (PendingWanted).

    The mod-side push is only the in-game enforcement carrier; this is the
    authoritative check for backend decisions (login re-assert, command
    gates). PendingWanted counts so a logout/login inside the 30s grace
    window keeps the lock: the re-assert used to compute effective from the
    Wanted table only and silently unlock a graced player.
    """
    return await teleport_lock_blocks(character) is not None


async def sync_no_teleport(character, http_client_mod) -> None:
    """Push the EFFECTIVE block set for one character.

    Replaces the [R] name tag as the teleport-lock carrier (freeman
    2026-09-23): on-duty police and wanted suspects are flagged invisibly.
    Authoritative — computes from current DB state and pushes the result, so
    it is safe to call at any transition point (login, activate/deactivate,
    wanted create/expire/arrest-clear, pending-wanted apply/drop).
    """
    blocks = await teleport_lock_blocks(character)
    # Invalidate the wanted-tick's transition cache: whatever we push here
    # (login, duty change, wanted create/clear, /noteleport), the next tick
    # must re-push its own distance-gated state instead of trusting a stale
    # "already pushed" entry.
    _pushed_lock_state.pop(character.guid, None)
    await push_no_teleport(character, http_client_mod, blocks)
