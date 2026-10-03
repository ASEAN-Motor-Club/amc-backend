# Illegal-TT mirrors: +14d windows + /setup_event duplicate SEs (PLAN DRAFT — do not implement until Yuuka's details arrive)

## Prod data (2026-10-03, asean-mt-server prod DB)

Active SEs with 14-day windows (the complaint):
- SE 73 `OjiNorthTT-V1 - IR - 110`  start 10-02 19:46 UTC, end 10-16 (yesterday's auto-post mirror — never closed)
- SE 74 `Swapped Cargo TT - IR - 140` start 10-03 01:00:15 (today's auto-post mirror), end 10-17
- SE 75 `OjiNorthTT-V1 - IR - 140` start 10-03 10:56:22, end 10-17  (a /setup_event post)
- SE 76 `OjiNorthTT-V1 - IR - 140` start 10-03 10:56:55, end 10-17  (another /setup_event 33s later)

All four are `is_rotation_instance=True` mirrors, tt_class 5 / 1, championship 3.
Template SEs 58–71 are CORRECTLY windowed 24h (2026-10-03 01:00 UTC → 10-04 01:00 UTC
= 08:00 → 08:00 +07, the daily reset) by `_rotation_reset` in post_random_events.

Matching GameEvents: 7421 + 7422 (state 1, auto_created=false, tt_class 140, both
linked scheduled_event_id=73 — the template-side link via setup-hash fallback).
No GameEvent row with guid prefix 25ab5408* exists in the DB (nothing matched).

## Root cause 1 — 14-day mirror windows

`amc/events.py::_mirror_posted_event` (lines 1014–1049) hardcodes:
```
start_time=timezone.now(),
end_time=timezone.now() + timedelta(days=14),   # line 1041
```
Called from BOTH posting paths: `setup_event` (:170) and `post_random_events` (:1409).
Nothing ever closes stale mirrors: the daily template re-window
(:1294–1301) filters `is_rotation_instance=False`, so mirrors are untouched.
Result: every posted instance leaves an active-14-days SE row behind.

## Root cause 2 — /setup_event creates a new active SE per post

Shipped rule "(a)" (2026-10-01 rework): after a successful /setup_event POST,
`_mirror_posted_event` creates ONE NEW mirror SE per posted instance, no dedupe.
Two invocations in one window → two active SEs (75, 76). Yuuka's agreed behavior:
/setup_event should just create the race event based on the active SE — no new SE.

Why the mirror exists at all: `/events` popup resolves the live event's SE via the
re-serialized setup hash; without a mirror, /events showed nothing (prod 7387,
2026-10-02) and the popup sat empty.

## Proposed fix (draft, minimal surface)

1. Mirror window = the DAILY WINDOW, not +14d: in `_mirror_posted_event`, set
   `start_time = _rotation_reset(now)` and
   `end_time = _rotation_reset(now) + timedelta(days=1)` — same window as
   templates; mirrors die at the next 08:00 +07 reset automatically.
2. `/setup_event` does NOT create a new SE per post — instead reuse/refresh the
   ONE active mirror of that window for the same template/rolled class
   (agreed: 'no duplicates'). Needs Yuuka's detail on the exact key: per window
   (one mirror regardless of track?) vs per (template, window) vs per (class, window).
3. One-time prod data cleanup: close the stale mirrors (SE 73/74/75/76) by
   setting their end_time to the current window end (or now), then let the
   mirrors regenerate correctly.

## Open questions for Yuuka
- Q1: mirror reuse key — one SE per daily window globally, or per posted instance
  but all sharing the same 24h window (windows identical, rows differ)?
- Q2: keep mirroring on the AUTO-post path at all, or does /events stop keying
  on SE rows (deeper change) — the mirror was the popup-empty fix.
- Q3: cleanup of the four stale rows — close all to window end, or delete?
