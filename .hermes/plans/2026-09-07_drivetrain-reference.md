# Stock drivetrain reference + /check_parts drivetrain line upgrade

**Date:** 2026-09-07 · **Requester:** Yuuka · **Companion:** MTDediMod PR #19
(`feat/last-vehicle-driveinfo` — attaches `GetDriveInfo` to the `/last` payload;
without it the line can only ever render `Unknown`).

## Design (per Yuuka)

- Same nature as unknown-parts: compare a **server-readable payload** against a
  **stock baseline**, no telemetry, no behavioral inference.
- Demand: know whether the drivetrain **differs from vanilla**. `Unknown` is an
  acceptable output; a misleading "stock" claim is not.
- Boundary (verified): **client-side-only** AWD paks never replicate their
  definition — no server-readable surface differs, so they are undetectable by
  design. The popup therefore never claims "stock/unmodified"; a matching
  layout is reported as `(server-side)`.
- Integrated into `/check_parts` (same command), upgrading the existing
  Drivetrain line — not a separate command.

## Data

`src/amc/data/stock_drivetrain.json` — 96 vanilla vehicles, extracted from the
mt-pak-extract parses (`out/*_parsed.json`, host) by counting
`Differential*_GEN_VARIABLE` component exports per vehicle definition.
Distribution: 73×1 diff, 18×2, 5×3 (Neo/Jemusi = stock 3-diff AWD).
Panther stock = **1** diff → the AWD pak's 3 diffs is a clean 1→3 signal.

## Popup semantics (`format_driveline_checked`)

| Case | Output |
|---|---|
| DriveInfo missing | `Drivetrain: Unknown` |
| live diffs ≠ stock reference | `Drivetrain: AWD — 4/4 wheels … [modified: 3 diffs, stock 1]` |
| live == stock (or no reference / no num_differentials) | `Drivetrain: RWD — 2/4 wheels … (server-side)` |

`(server-side)` is a scope statement, never a cleanliness verdict.

## Validation still owed

First live DriveInfo from the mod change must confirm the runtime
`num_differentials` equals the static blueprint count (Panther stock: expect 1).
If live counts differ structurally, recalibrate the reference before trusting
the mismatch flag.

## Changes

1. `src/amc/data/stock_drivetrain.json` — extracted reference.
2. `src/amc/vehicles.py` — `load_stock_drivetrain()` (cached, degrades to {}),
   `stock_model_from_class_name()`, `format_driveline_checked()`.
3. `src/amc/commands/vehicles.py` — `/check_parts` uses the checked line.
   `/check_mods` keeps the plain line (scope).
4. Tests: seed integrity, model extraction, four comparison cases, popup
   modified-flag case; existing popup test updated for the qualifier.
