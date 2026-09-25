# Plan: Stock vs Flow — sector health that rewards activity

**Date:** 2026-09-25 · **Status:** proposed (freeman: "spec it into a plan")
**Branch:** `feat/econ-stock-flow` (from origin/master @ 7e8fa25, clean tree)

## Problem

Filling % measures stock; draining a DP's output is flow. Today:

- Sector fill is computed over INPUT rows only (since #220's rows query),
  but a drained OUTPUT still reads as "the site has less" in the
  starved/fill story users see, and
- the contribution score credits only deliveries that fill an INPUT
  deficit — the hauling half of the production loop (emptying an
  OUTPUT) scores zero, so emptying a DP *lowers* perceived economy
  health even though economic activity is exactly what we want.

## Design

Two independent measures per sector; never let activity subtract.

### 1. Stock fill (existing, tightened)

- Keep current INPUT-only capacity-weighted fill (uses
  `effective_capacity` incl. Pallet-50 category default from #221).
- Explicit invariant: OUTPUT rows are out of both the numerator and the
  denominator of fill. (Already true post-#220; add a regression test.)
- Starvation stays input-only (Noksan rule from #220: an INPUT row is
  starved only if no same-cargo OUTPUT stock exists).

### 2. Throughput (new)

- Per DP: sum of delivered units *out of* the DP in the trailing 24h —
  `amc_delivery.sender_point_id` grouped by sender, legal cargo only
  (LEGAL_CARGO_FILTER), same illicit exclusion as everywhere else.
- Per sector: throughput = Σ outbound units of sector DPs. Normalize as
  a "velocity": throughput ÷ (sector total INPUT capacity-days × k), so
  a big sector isn't favored by size alone. Calibrate k so that a
  sector moving its own input capacity once per day ≈ 1.0.
- Window 24h, matching the contributors endpoint.

### 3. Headline health

`health = fill + throughput_bonus`, `throughput_bonus = bonus_cap ×
min(1, velocity)` — proposed `bonus_cap = 15` points.

Invariants:

- Activity never subtracts: `health ≥ fill` always.
- Fill/throughput each still surfaced separately (embed shows
  `fill 62% · flow 1.2k/d`; page shows both + bonus).
- A sector with zero deliveries loses its bonus but never drops below
  its fill.

## Files

- `src/amc/economy_dashboard.py`
  - `sector_outbound_units(window_start, window_end)` — one grouped
    query on `amc_delivery` by `sender_point_id`.
  - `sector_health()` — add `throughput`, `velocity`, `flow_bonus`,
    `health` fields; fill/starved logic unchanged.
  - `sector_drilldown()` — per-site `flow_24h` on each site row.
- `src/amc/economy_weights.py` — no change (weights untouched; the 0.15
  hop-discount proposal is ON HOLD per freeman).
- `src/amc/api/v1/schema.py` — extend sector-health + drilldown schemas
  (new additive fields only).
- `src/amc_cogs/` economy embed — render `flow` line.
- `/opt/data/workspace/economy-live.html` — per-sector flow badge +
  per-site flow column.
- Tests: outbound-units query, `health ≥ fill` invariant, bonus cap,
  drilldown flow field, illicit sender exclusion.

## Knobs (constants at top of economy_dashboard.py)

- `FLOW_BONUS_CAP = 15.0`
- `FLOW_WINDOW_HOURS = 24`
- velocity normalization constant `k` (calibrated so daily-full-churn = 1.0)

## Not in scope

- Hop-discount rework (0.3 → 0.15) — on hold, freeman decision pending.
- Anything that makes haulers *lose* points for draining outputs
  (outbound hauling already earns contribution via its own delivery's
  INPUT deficit elsewhere; no double-credit for now).

## Ship path

PR → CI pytest → merge go → amc-server pin bump → deploy `--restart-be`
→ verify prod `/api/v1/economy/sectors/` shows flow fields and embed
re-renders.
