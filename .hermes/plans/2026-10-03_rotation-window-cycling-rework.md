# Rework: rotation = SE window cycling (B2) — no auto-post, no mirrors

Status: FINAL for Yuuka review. B2 confirmed by Yuuka 2026-10-03 ("B2. Give me the full plan"). No implementation started.

## Model (Yuuka's authoritative description of the ORIGINAL system)

- ScheduledEvents are visible in the admin DB. The ONLY activeness mechanism is the
  `[start_time, end_time]` window: within the window → available to `/setup_event`,
  and `/events` shows its description. No other gating.
- There is NO mirroring and NO auto-post. Every historical event (AMC Cup SS2/SS3 TTs)
  was manually made and manually launched.
- The class is decided at ROTATION time: the daily 08:00 UTC+7 tick rolls the HP class
  ONCE for the day and the activated SE carries it; `/setup_event` posts the event as
  decided — it never rolls, never synthesizes anything.
- All current issues are self-inflicted deviations (auto-post, mirrors, template
  exclusion, stripped-duplicate pool). Fix = remove the deviations.

## Verified prod facts anchoring the plan

- Cup-era originals carry authored descriptions (SE 34/36/38 etc.); underground
  "templates" 58–71 are stripped duplicates of them (same setups, descriptions
  EMPTIED) lumped into championship 3 ("Jeju Underground Street Racing").
- SE 77 is today's one mirror (`is_rotation_instance=True`, IR - 350).
- `/setup_event` class logic today: pinned SE class → use it; classless underground
  template → sticky via newest classed GameEvent in window; else roll. (events.py:84-117)
- The game purges owner-less events within minutes; player-owned events persist
  (OjiNorth posts from 18:25 UTC were player-owned and remained until ended).
- The #311 `/events` exclusion (hide classless underground templates) is the hole the
  mirror system was invented to paper over (#323, then one-per-window #329).
- GameEvent↔SE link: posted setup hash + window match (handlers/events.py:117-135),
  with an owner-less out-of-window fallback. With no auto-post, that fallback's
  purpose dies; the windowed match keeps working for /setup_event posts.

## Changes (one PR, amc-backend)

1. **Retire the auto-POST + rotate-out branch of `post_random_events`** (amc/events.py
   ~1236-1424): the tick keeps its schedule (08:00:15 +07 cron unchanged in worker.py)
   but its body becomes pure rotation state management — see 2. The mod POST path,
   `active_auto` counting, candidate pool query, `(NNN)` instance counter, and the
   rotate-out/unclaimed-removal reconcile all go away. Announce stays (reworded).
   - Keep the function name `post_random_events` (cron registration + tests key on
     it); the body is now the rotation. Docstring states the semantic change.
     Manual in-process trigger recipe unchanged (proven today).
2. **Rotation body (B2 core), in this order:**
   a. `window_start = _rotation_reset(now)`; `window_end = window_start + 1d`.
   b. Pick the ACTIVE SE for the window:
      - Pool = original SEs with race_setup, NOT the stripped 58–71 duplicates —
        pool gates: `race_setup__isnull=False`, championship 3, tt_class NULL,
        `is_rotation_instance=False`, 0-lap setups (NumLaps absent == 0, Python-side
        filter — sprint rule kept).
      - **Cycling order: round-robin.** Order pool by SE id; the slot comes from the
        PREVIOUS window's active SE (the underground SE whose window ended inside the
        previous window) → next id in the cycle, wrapping. Deterministic, restart-safe,
        no state file. First run after deploy starts at pool[0].
   c. **Close every other underground SE window**: `end_time = now() - 1min` for all
      underground-championship SEs that are NOT the picked one. Never touch
      non-underground SEs (cup flow, Discord-synced rows).
   d. **Roll the class ONCE**: random TTClass; write it ONTO the picked SE
      (`asave(update_fields=["tt_class"])`) — the SE carries today's class;
      `/setup_event` hits its pinned-class branch and never rolls.
   e. **Window the picked SE**: `start_time = window_start`, `end_time = window_end`.
   f. **Announce** (in-game chat via `announce()`, unchanged channel): "Today's
      underground event: <SE name> - IR - <HP>. Use /events to see it."
3. **`setup_event` simplification** (amc/events.py:52-173):
   - Class logic collapses to: pinned `scheduled_event.tt_class` → use it; else
     classless (never criminalized). DELETE the sticky-via-GameEvent-history block
     (:84-117), the select_related re-fetch, and the roll fallback.
   - DELETE `_mirror_posted_event` + both call sites (the #311 `/events` exclusion is
     removed, so the popup-empty hole does not exist).
   - KEEP: the IR name tag `"<SE name> - IR - <HP>"` when the SE carries a class
     (native-template defeat still holds — the class tag makes the name unmatchable).
     DELETE the `(NNN)` per-instance counter (`_next_tt_instance_number`): under the
     original model the posted event is just the SE's name; two same-SE posts same-day
     share the name (same property as the original system).
   - KEEP: config deep-copy before the Location→Translation rewrite (mutation trap),
     and restriction clearing (`VehicleKeys`/`EngineKeys` = []) at post time.
4. **`cmd_events_list`** (amc/commands/events.py:184-207): DELETE the underground
   exclusion — restore the original "list every windowed SE" behavior. With the
   rotation closing all-but-one underground window, the listing naturally shows
   exactly today's event (+ any manually-windowed SEs, which is correct cup behavior).
5. **handlers/events.py**: the owner-less out-of-window SE fallback (added for
   auto-posted events, Yuuka 2026-09-30) becomes dead — REMOVE it. Player posts keep
   the windowed match; a rogue setup copy must not inherit an SE.
6. **Dead code cleanup (same PR)**: `_next_tt_instance_number`,
   `_mirror_posted_event`, the underground template re-window block
   (events.py ~1294-1301, replaced by the rotation body). `_rotation_reset` STAYS
   (rotation body + enforcement use it). `UNDERGROUND_CHAMPIONSHIP_NAME` stays
   (payouts key on the championship).
7. **Blood Money / payouts**: `pay_underground_rotation_rewards` keys on
   championship + GameEvent, NOT on mirrors — unchanged. rewards_paid path stays.
8. **Enforcement**: DQ/wanted/police read `GameEvent.tt_class` via the IR name tag
   parse-back — untouched by all of the above.

## Data work (prod, after deploy — with Yuuka's go)

- Delete mirror rows: SE 77 (+ any the first post-rotation tick creates — none should,
  mirrors are gone).
- Stale state-1 GameEvent rows with no live guid (7427/7428 verified 404 in-game)
  → state=3. Bounded query reviewed before running.
- Stripped duplicates 58–71: DELETE (setups remain; originals carry descriptions).
  First DIFF each duplicate's setup vs its original's — any that differ stay as
  new original-style SEs WITH a proper description (report the diff to Yuuka).
- Pool membership is data, not code: print the final pool in the deploy report.

## Tests (same PR)

- Rotation: ONE active underground SE windowed to the daily reset + tt_class written
  onto the SE row; round-robin sequence (two consecutive ticks → next id, wrap case);
  "close the others" (only picked SE stays windowed); announce text.
- `setup_event`: pinned class → IR name tag, no roll; classless SE → classless post,
  no mirror, no `(NNN)`; config-copy non-mutation test stays.
- Delete: mirror tests (reuse/one-per-window), per-instance-name tests, sticky-class
  tests (mechanism removed).
- Grep the whole test tree for `is_rotation_instance` / `_mirror_posted_event` /
  `_next_tt_instance_number` / `[TT-` / ` - IR - ` — every referencing file updated in
  one PR (test_tt_police.py strip helpers included).
- Scoped runs locally via iso-pytest.sh; full suite in CI.

## Ship chain (standard)

PR → CI green → Yuuka merge go → pin-bump PR → prod deploy → verify RUNNING env
(marker grep + process environ; restart services explicitly if stale) → data work
(with go) → manual rotation trigger for today (in-process recipe, proven today) →
verify live list + /events + /setup_event with Yuuka watching.

## CORRECTION (Yuuka 2026-10-03, third pass): leave SEs 58-71 untouched

- Yuuka: "do not modify the original SEs, those duplicates are made for the
  very reason." The duplicates ARE the rotation pool by design (duplicating
  kept the cup-era originals pristine). Plan step "delete 58-71" is DROPPED.
- Descriptions on 58-71 were EMPTY ON PURPOSE: the underground description is
  POST-TIME synthesized (_underground_description, events.py:992) — the
  auto-poster/mirror wrote the class-specific requirements text onto the
  mirror, never onto the template. Templates were seeded blank because the
  description belongs to the POST, not the SE.
- Under B2 (no auto-post) there is no post-time writer, so WHERE does the
  class-specific requirements text come from? The `/events` popup shows
  `description_in_game or description` of the LISTED SE — today's active SE
  (58-71) is blank → the popup would show nothing. Options:
    (i) The rotation tick writes the SAME _underground_description text onto
        the ACTIVE SE (description_in_game only) each window — description is
        per-window state like tt_class, next tick overwrites. Equivalent to
        today's behavior, just on the template instead of a mirror.
        Requires modify-timing-only change: write description_in_game at the
        SAME tick that writes tt_class + window (so the "original" stays
        unmodified in spirit: nothing persists after the window; next rotation
        rewrites it).
        CAVEAT: it overwrites the previous day's text on that SE — SE data
        changes daily. "Do not modify" = the ORIGINALS 34/36/38/51 stay
        untouched; 58-71 are rotation slots whose description_in_game is
        WINDOW STATE. Confirm this reading with Yuuka if unsure.
    (ii) Keep a generic static description on 58-71 (e.g. "Underground street
        race — use /events for today's class") and put the class/requirements
        in the posted event NAME tag + the in-game popup requirements
        (VehicleKeys/EngineKeys stay cleared — DQ owns enforcement). No
        description writes at all.
- RESOLVED (Yuuka 2026-10-03): **(i) tick-written description** — "description IS
  the thing that's shown by /events. Editing 58-71 is okay." The rotation tick
  writes `_underground_description(tt_class, checkpoints)` onto the ACTIVE SE's
  `description_in_game` (+ description) in the same write block as tt_class +
  window. Overwritten every tick; 58-71 carry only per-window state, the
  cup-era originals 34/36/38/51 are never touched.
- ROTATION POOL (final): SEs 58-71 (+ 51? NO — 51 is a cup original with a
  pinned class, NOT a pool member; leave it out unless Yuuka adds it). Pool
  gates stay as written in change 2b minus any "originals" language.


- The 08:00:15 +07 cron slot; the 08:30 restart; enforcement/DQ/wanted; payouts;
  Discord scheduled-event sync (cup flow, on_scheduled_event_create/update);
  `/results`, `conclude_event`, `staggered_start`; TTClass ladder; vanilla-tire rule.
