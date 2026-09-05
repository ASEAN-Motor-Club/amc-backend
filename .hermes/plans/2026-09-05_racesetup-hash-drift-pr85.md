# RaceSetup hash int/float drift — TT grouping fix (PR #85)

**Date:** 2026-09-05
**PR:** ASEAN-Motor-Club/amc-backend#85 (`fix/racesetup-hash-normalize`)
**Requested by:** Yuyou (YUUYU [DOT]) — "lets prep PR B, then we have a summary for yuuka"
**Status:** pushed, awaiting review/merge by Yuuka. NOT deployed. Production is under a
deploy freeze while Discord integration testing runs (Yuyou, 2026-09-05).

## Problem

Motor Town re-emits race setup configs with **float** leaves (`0.0`, `10.0`)
while the older stored pipeline used **int** leaves (`0`, `10`).
`RaceSetup.calculate_hash` = raw `DeepHash` over the structure → the two
variants hash differently → content-identical setups land in separate rows.

Production evidence (2026-09-05 test session, main server):

- `ScheduledEvent #38` ("Electric Speed Trap - TT") referenced int setup **807**.
- The game re-emitted the identical route as float setup **848**; every session
  recorded against 848.
- `filter_by_scheduled_event` (TT mode) matches on `race_setup` + window →
  **0 sessions grouped → 0 points, 0 prizes, no errors anywhere.**

A side finding (2026-09-05, corrected same day): the owner-less event crash
(`ValueError: Field 'unique_id' expected a number but got ''` — the mod sends
`OwnerCharacterId: {'UniqueNetId': '', ...}` and the handler filters
`player__unique_id=''` → `int('')`) is **already fixed in open PR #83**
(`fix/event-run-row-resolution`), along with natural-finish detection and
per-run guid resolution. That PR also explains the `finished=False` anomaly
from the same test session. This PR (#85) does **not** overlap #83 — disjoint
files, both baseline-green, safe to land in either order.

## Fix in #85

1. `RaceSetup.normalize_config` — recursive int→float coercion; **bools
   preserved** (bool ⊂ int in Python; `True` must not collapse into `1.0`).
2. `calculate_hash` hashes the normalized structure.
3. **Data migration 0233** — re-hash all rows, merge duplicates to the
   lowest-id survivor, re-point `GameEvent` + `ScheduledEvent` FKs
   (the only two FKs to `RaceSetup`). Two-phase hash update protects the
   unique constraint. `config IS NULL` rows untouched. Idempotent. Reverse is
   a no-op.

## Verified

- 8 new tests (`src/amc/test_racesetup_hash.py`) — hash equality, bool
  preservation, config separation, model↔migration agreement, merge +
  FK re-point, null-config safety, idempotency. All pass (throwaway PG stack).
- Regression: `test_event_handlers.py`, `test_events.py`,
  `test_events_cog.py`, `test_autocomplete.py` — 34 passed.
- ruff: new files clean; `models.py` error profile identical to master.

## Deploy implications (for Yuuka's gate)

- **Backend-only. No game-server restart.** Migration runs at deploy.
- After deploy, prod setups 807/848 collapse to one row and the manual
  workaround (schedule #38 → 848) self-heals — both ids resolve to the
  survivor. Workaround row should be left as-is; it converges on its own.
- `RaceSetup.hash` values change for most rows. API routes that look setups
  up by hash resolve via the new values; no persisted client state depends
  on old hash strings.

## Follow-ups (not in this PR)

- **Bug A:** guard empty-string `UniqueNetId` in `_upsert_game_event`
  owner resolution (`src/amc/handlers/events.py` ~line 100) so owner-less
  events dispatch instead of crashing. Prereq for the auto-TT pipeline
  (`post_random_events`) to be visible.
- Consider surfacing `best_lap_time` in TT embeds (lap-based TTs only;
  sprint TTs like Electric Speed Trap are point-to-point — completed
  `net_time` is the right metric there).
