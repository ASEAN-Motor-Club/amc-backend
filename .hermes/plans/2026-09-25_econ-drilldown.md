# Plan: Sector Drilldown + Illicit Cargo Exclusion (State of the Economy)

Branch: `feat/econ-drilldown` off amc-backend master (`e86b6ed`).
One PR for the illicit exclusion + drilldown API (backend), one for the page (static file, no repo) — actually the page lives outside any repo (`/var/lib/mod-releases`), so backend PR + direct page update, and the cog chart unchanged except the illicit filter.

## 1. Exclude illicit cargo (everywhere)

Single source of truth: `ILLICIT_CARGO_KEYS` in `src/amc/special_cargo.py`
(Money, Ganja, CocaLeavesPallet, GanjaPallet, Cocaine, MoneyPallet,
Moonshine, CocaPaste, CocaineBricks).

- `economy_dashboard.py`:
  - `snapshot_storages()`: filter `~Q(cargo_key__in=ILLEGAL)` — illicit stock
    never enters snapshots (keeps StorageSnapshot small and every consumer clean).
  - `contribution_leaderboard()`: add `cargo_key__in=ILLEGAL` to the Delivery
    filter exclusion — illicit hauls score 0. Scores recompute retroactively
    (windows read Deliveries), so the 7d/30d boards shift immediately.
  - `sector_health()`: already reads snapshots; the snapshot filter covers it,
    but add the same exclusion to any live-read path for belt & braces.
- `economy_weights.py`: no change needed (weights are per cargo; illicit cargo
  simply never reaches the pipeline) — but add an assert-style guard in
  `weight_of` returning 0 for illicit keys, so any future caller is safe.
- Drilldown (new, below) inherits the exclusion via the snapshot filter.
- Tests: illicit cargo in fixtures → absent from sectors/leaderboard/sites.
- Note: this is a data-visible change — the live page, cog chart, and boards
  all change the moment this deploys (expected: small fill %, score shifts for
  anyone hauling ganja/cocaine).

## 2. Sector drilldown — DPs + storages

Sector is a property of CARGO (`economy_weights.SECTORS`), so a DP belongs to
a sector iff it has INPUT storages whose cargo maps into it (Steel Mill shows
under mining via ore/coal inputs — correct and already how `sector_health`
counts fill).

### API — `GET /api/v1/economy/sectors/{sector}/`

Response:
```json
{"sector": "mining", "fill": 0.52,
 "sites": [
   {"guid": "...", "name": "Iron Mine", "type": "mine",
    "fill": 0.41, "starved": true,
    "storages": [
      {"cargo": "IronOre", "kind": "IN", "amount": 300, "capacity": 1000},
      {"cargo": "Fuel", "kind": "IN", "amount": 20, "capacity": 60}
    ]}
 ]}
```

- Reads LIVE `DeliveryPointStorage` joined to `DeliveryPoint` (current state,
  not snapshots — drilldown is "look now", snapshots stay for trends/history).
- Ordered starved-first then by fill ascending; `limit` param (default 100,
  cap 400) + `starved_only` param.
- Per-site `fill` = unit-weighted over its INPUT rows in that sector.
- Illicit cargo rows excluded (section 1).
- 404 on unknown sector; `other` sector reachable too.

### Web page (`economy.html`)

- Sector row becomes click-to-expand: DP list (name, fill bar, starved badge),
  each DP click-to-expand → its storages table (cargo, IN/OUT, amount/capacity,
  inline fill %).
- Vanilla JS, no framework (page stays a single static file); reuse existing
  color bands. Loading on expand (fetch per sector, cached client-side).
- Mobile: expansion rows reflow under the existing 640px breakpoint.

### Cog chart

- No expansion possible in a static image — instead: worst-site name per
  sector in the chart? No — clutter. Chart stays as-is; the embed URL already
  points at the page where drilldown lives. Only change: illicit exclusion
  changes the numbers automatically.

## 3. Tests

- `test_economy_dashboard.py` additions:
  - illicit storage rows never snapshot; illicit deliveries don't score
  - drilldown endpoint: sector grouping, starved-first order, IN/OUT storages,
    exclusion of illicit rows, 404, limit
- Existing tests updated where fixtures used illicit cargo.

## 4. Ship

1. PR amc-backend → merge → pin bump amc-server → deploy prod (`--restart-be`)
2. Update `economy.html` on amc-peripheral (`/var/lib/mod-releases/`) + sha verify
3. Verify: sectors endpoint gains `sites`; page expand works; ganja/cocaine
   absent from boards (spot-check a known illicit hauler drops off the 7d board)

## Open question

- Wanted-board names on the drilldown: DP names are site names (public map
  data), not player names — no privacy concern there. Contributor board is
  unchanged.
