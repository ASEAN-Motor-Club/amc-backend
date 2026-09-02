"""CLI: python -m powercalc <setup|recommend|parts|engines|version> ..."""

from __future__ import annotations

import argparse
import json
import sys

from . import (
    PartNotFound,
    compute_setup,
    data_version,
    list_engines,
    list_parts,
    model_version,
    provenance,
    search,
)


def _emit(obj, as_json: bool) -> None:
    if as_json:
        json.dump(obj, sys.stdout, indent=1, default=lambda o: o.as_dict())
        print()
    else:
        print(json.dumps(obj, indent=1, default=lambda o: o.as_dict()))


def _cmd_setup(args) -> int:
    try:
        res = compute_setup(
            args.engine,
            intake_part=args.intake,
            turbo_part=args.turbo,
            keep_points=args.points,
        )
    except PartNotFound as e:
        print(f"unknown part: {e}", file=sys.stderr)
        return 2
    if args.json:
        _emit(res.as_dict(), True)
        return 0
    tag = "EV" if res.is_ev else f"{res.fuel}/{res.curve}"
    print(f"=== {res.engine_part} ({res.engine_asset}) — {tag} ===")
    print(f"Peak power:  {res.peak_power_hp:.1f} hp @ {res.peak_power_rpm:.0f} rpm")
    print(f"Peak torque: {res.peak_torque_nm:.1f} Nm @ {res.peak_torque_rpm:.0f} rpm")
    if res.mass_kg is not None:
        print(f"Engine mass: {res.mass_kg:.0f} kg")
    return 0


def _cmd_recommend(args) -> int:
    branches = args.branch or [None]
    hits = []
    for b in branches:
        hits.extend(
            search(
                args.target_hp,
                tolerance=args.tolerance,
                branch=b,
                categories=args.category,
                min_mass=args.min_mass,
                max_mass=args.max_mass,
                limit=args.limit,
                sort=args.sort,
            )
        )
    hits.sort(key=lambda h: (abs(h.peak_power_hp - args.target_hp), h.cost, -h.peak_torque_nm))
    hits = hits[: args.limit]
    if args.json:
        _emit([h.as_dict() for h in hits], True)
        return 0
    if not hits:
        print("no combinations in range — widen tolerance or filters")
        return 1
    print(f"{'hp':>6} {'@rpm':>6} {'Nm':>7} {'@rpm':>6}  {'engine':<30} {'kg':>5} {'intake':<19} {'turbo':<28} $")
    for h in hits:
        print(
            f"{h.peak_power_hp:6.1f} {h.peak_power_rpm:6.0f} {h.peak_torque_nm:7.0f} "
            f"{h.peak_torque_rpm:6.0f}  {h.engine_part:<30} {h.mass_kg:5.0f} "
            f"{h.intake_part or '(stock)':<19} {h.turbo_part or '(none)':<28} {h.cost}"
        )
    return 0


def _cmd_parts(_args) -> int:
    _emit(list_parts(), False)
    return 0


def _cmd_engines(args) -> int:
    _emit(list_engines(args.category, args.min_mass, args.max_mass), False)
    return 0


def _cmd_version(_args) -> int:
    p = provenance()
    v = p.get("validation", {})
    print(f"model: {model_version()}  data: {data_version()}")
    print(f"validated: {v.get('method')}")
    print(f"  in-game {v.get('in_game')}")
    print(f"  model   {v.get('model')}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="powercalc", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("setup", help="calculate an exact engine+intake+turbo setup")
    p.add_argument("engine", help="engine part id")
    p.add_argument("--intake", help="intake part id (default: stock)")
    p.add_argument("--turbo", help="turbo part id (default: none/NA)")
    p.add_argument("--points", action="store_true", help="include torque curve points")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_setup)

    p = sub.add_parser("recommend", help="recommend builds for a target hp")
    p.add_argument("target_hp", type=float)
    p.add_argument("--tolerance", type=float, default=4.0)
    p.add_argument("--branch", action="append", choices=["na", "turbo", "eco", "ev"])
    p.add_argument("--category", action="append", choices=["car", "pickup", "truck", "bike"])
    p.add_argument("--min-mass", type=float)
    p.add_argument("--max-mass", type=float)
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--sort", choices=["closeness", "torque", "cost"], default="closeness")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_recommend)

    p = sub.add_parser("parts", help="list intake/turbo parts with baked values")
    p.set_defaults(func=_cmd_parts)

    p = sub.add_parser("engines", help="list engine part rows")
    p.add_argument("--category", action="append", choices=["car", "pickup", "truck", "bike"])
    p.add_argument("--min-mass", type=float)
    p.add_argument("--max-mass", type=float)
    p.set_defaults(func=_cmd_engines)

    p = sub.add_parser("version", help="model/data versions + validation case")
    p.set_defaults(func=_cmd_version)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
