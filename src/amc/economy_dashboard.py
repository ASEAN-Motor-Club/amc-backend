"""State-of-the-Economy dashboard: storage snapshots + contribution scoring.

Data model:
- `StorageSnapshot` (amc.models) — hourly point-in-time copy of
  DeliveryPointStorage, written by the worker cron `snapshot_storage_tick`.
- `contribution_leaderboard()` — per-character contribution score for a
  window. Score per delivery = min(units, deficit_before) * weight(cargo),
  where deficit_before is the destination site's INPUT shortfall
  (capacity - amount) taken from the latest snapshot at or before the
  delivery timestamp. Fallbacks (no snapshot / no capacity): full credit
  for the delivered units (neutral starvation weight).

Health metric:
- `sector_health()` — per sector, sum(amount) / sum(capacity) over current
  INPUT storages whose cargo maps into the sector (see economy_weights).
"""

import bisect
import logging

from django.db import models
from django.utils import timezone

from amc.economy_dashboard_cargo import effective_capacity
from amc.economy_weights import sector_of, weight_of
from amc.models import Delivery, DeliveryPointStorage, StorageSnapshot
from amc.special_cargo import ILLICIT_CARGO_KEYS

logger = logging.getLogger("amc.economy_dashboard")

MAX_HAUL_DEGREE = 1.0  # starvation weight when no snapshot/capacity data exists

# Illicit cargo is out of the economy dashboard entirely: it never enters
# storage snapshots, never scores on the contribution board, never counts
# toward sector fill. Single source of truth = special_cargo.ILLICIT_CARGO_KEYS.
LEGAL_CARGO_FILTER = ~models.Q(cargo_key__in=ILLICIT_CARGO_KEYS)


async def snapshot_storages() -> int:
    """Copy the current storage table into StorageSnapshot. Returns row count."""
    now = timezone.now()
    current = [
        StorageSnapshot(
            delivery_point_id=dp_id,
            kind=kind,
            cargo_key=cargo_key,
            amount=amount,
            capacity=capacity,
            captured_at=now,
        )
        async for dp_id, kind, cargo_key, amount, capacity in DeliveryPointStorage.objects.filter(
            LEGAL_CARGO_FILTER
        ).values_list("delivery_point_id", "kind", "cargo_key", "amount", "capacity")
    ]
    await StorageSnapshot.objects.abulk_create(current, batch_size=2000)
    logger.info("storage snapshot written: %d rows", len(current))
    return len(current)


def _deficit_timeline(rows: list[tuple[int, float | None, int, int]]):
    """Build (timestamps, deficits) per (point, cargo) from snapshot rows.

    rows: (captured_at_epoch, capacity, amount, kind) sorted by captured_at.
    Returns two parallel lists: sorted capture times and the deficit
    (capacity - amount, floored at 0) in force AFTER that capture.
    """
    times: list[int] = []
    deficits: list[float] = []
    for epoch, capacity, amount, _kind in rows:
        deficit = 0.0
        if capacity and capacity > 0:
            deficit = max(0.0, capacity - amount)
        else:
            deficit = -1.0  # sentinel: no capacity data
        times.append(epoch)
        deficits.append(deficit)
    return times, deficits


async def contribution_leaderboard(window_start, window_end=None, limit: int = 20):
    """Per-character contribution score over a window.

    Returns list of dicts: {character_id, name, units, payment, score}.
    """
    if window_end is None:
        window_end = timezone.now()

    deliveries = [
        (
            d["character_id"],
            d["cargo_key"],
            d["quantity"],
            d["payment"],
            d["destination_point_id"],
            int(d["timestamp"].timestamp()),
        )
        async for d in Delivery.objects.filter(
            timestamp__range=[window_start, window_end],
            character__isnull=False,
        )
        .filter(LEGAL_CARGO_FILTER)
        .values(
            "character_id",
            "cargo_key",
            "quantity",
            "payment",
            "destination_point_id",
            "timestamp",
        )
    ]

    # snapshot timeline per (destination_point, cargo) covering the window
    pairs = {(d[4], d[1]) for d in deliveries if d[4]}
    timelines: dict[tuple, tuple[list[int], list[float]]] = {}
    if pairs:
        qs = StorageSnapshot.objects.filter(
            kind="IN",
            captured_at__gte=window_start,
            cargo_key__in={p[1] for p in pairs},
        )
        grouped: dict[tuple, list] = {}
        async for s in qs.values_list(
            "delivery_point_id", "cargo_key", "captured_at", "capacity", "amount"
        ):
            key = (s[0], s[1])
            grouped.setdefault(key, []).append((int(s[2].timestamp()), s[3], s[4], 0))
        for key, rows in grouped.items():
            rows.sort()
            timelines[key] = _deficit_timeline(rows)

    per_char: dict[int, dict] = {}
    for character_id, cargo_key, quantity, payment, dest, ts in deliveries:
        entry = per_char.setdefault(
            character_id, {"units": 0, "payment": 0, "score": 0.0}
        )
        entry["units"] += quantity
        entry["payment"] += payment

        weight = weight_of(cargo_key)
        credited = quantity
        if weight <= 0:
            # unclassified cargo: no score, still counted in units
            credited = 0
        elif dest:
            timeline = timelines.get((dest, cargo_key))
            if timeline:
                times, deficits = timeline
                idx = bisect.bisect_right(times, ts) - 1
                if idx >= 0:
                    deficit = deficits[idx]
                    credited = quantity if deficit < 0 else min(quantity, deficit)
                # idx < 0 (delivery before first snapshot): neutral full credit
            # no timeline at all: neutral full credit
        entry["score"] += credited * weight

    names = {}
    from amc.models import Character

    async for c in Character.objects.filter(id__in=per_char).values("id", "name"):
        names[c["id"]] = c["name"]

    board = [
        {
            "character_id": cid,
            "name": names.get(cid, "?"),
            "units": e["units"],
            "payment": e["payment"],
            "score": round(e["score"], 1),
        }
        for cid, e in per_char.items()
    ]
    board.sort(key=lambda x: -x["score"])
    return board[:limit]


async def sector_health():
    """Per-sector supply health from current INPUT storages.

    Returns list of dicts sorted by fill ascending:
    {sector, amount, capacity, fill, starved_sites} where starved_sites
    counts (site, cargo) INPUT rows at or below 15% fill.
    """
    # INPUT rows: (dp_id, dp_type, cargo, amount, capacity) — plus a map of
    # same-cargo OUTPUT stock per site: a site whose OUTPUT side holds stock
    # of the cargo isn't starved (for storage warehouses the output IS the
    # storage; only factories with a genuinely empty intake count).
    in_rows = [
        (
            r["delivery_point_id"],
            r["delivery_point__type"],
            r["cargo_key"],
            r["amount"],
            r["capacity"],
        )
        async for r in DeliveryPointStorage.objects.filter(kind="IN")
        .filter(LEGAL_CARGO_FILTER)
        .values(
            "delivery_point_id",
            "delivery_point__type",
            "cargo_key",
            "amount",
            "capacity",
        )
    ]
    # resolve unmetered rows to their cargo-category default capacity
    # (e.g. warehouse pallet stock: game doesn't meter it, Pallet default 50)

    in_rows = [
        (dp_id, dp_type, cargo_key, amount, eff)
        for dp_id, dp_type, cargo_key, amount, capacity in in_rows
        if (eff := await effective_capacity(cargo_key, capacity))
    ]
    out_stock: dict[tuple[int, str], int] = {}
    async for r in (
        DeliveryPointStorage.objects.filter(kind=DeliveryPointStorage.Kind.OUTPUT)
        .filter(LEGAL_CARGO_FILTER)
        .values("delivery_point_id", "cargo_key", "amount")
    ):
        key = (r["delivery_point_id"], r["cargo_key"])
        out_stock[key] = max(out_stock.get(key, 0), r["amount"])
    from amc.economy_weights import row_in_sector

    by_sector: dict[str, dict] = {}
    for dp_id, dp_type, cargo_key, amount, capacity in in_rows:
        sector = sector_of(cargo_key)
        if sector == "other" or not row_in_sector(sector, cargo_key, dp_type):
            continue
        s = by_sector.setdefault(sector, {"amount": 0, "capacity": 0, "starved": 0})
        s["amount"] += amount
        s["capacity"] += capacity
        # same-cargo OUT stock present → the site isn't starved for this cargo
        if amount / capacity <= 0.15 and out_stock.get((dp_id, cargo_key), 0) == 0:
            s["starved"] += 1
    out = []
    for sector, s in by_sector.items():
        fill = s["amount"] / s["capacity"] if s["capacity"] else None
        out.append(
            {
                "sector": sector,
                "amount": s["amount"],
                "capacity": s["capacity"],
                "fill": round(fill, 4) if fill is not None else None,
                "starved_sites": s["starved"],
            }
        )
    out.sort(key=lambda x: x["fill"] if x["fill"] is not None else 2)
    return out


async def sector_drilldown(
    sector: str, *, starved_only: bool = False, limit: int = 100
):
    """Expand one sector to its delivery points and their storages.

    A DP belongs to the sector iff it has INPUT storages whose cargo maps
    into the sector (same rule sector_health uses for fill). Reads LIVE
    DeliveryPointStorage — drilldown is "look now"; snapshots stay for
    trends/history. Illicit cargo rows excluded.

    Returns {sector, fill, sites: [...]} with sites ordered starved-first,
    then by fill ascending. Each site: {guid, name, type, fill, starved,
    storages: [{cargo, kind, amount, capacity}]} — storages sorted INPUT
    first, then amount/capacity fill ascending.
    """
    from amc.economy_weights import SECTORS

    if sector not in SECTORS and sector != "other":
        return None

    sector_cargo = set(SECTORS.get(sector, []))
    limit = max(1, min(limit, 400))

    storages = [
        {
            "dp_id": s["delivery_point_id"],
            "name": s["delivery_point__name"],
            "type": s["delivery_point__type"],
            "cargo": s["cargo_key"],
            "kind": s["kind"],
            "amount": s["amount"],
            "capacity": s["capacity"],
        }
        async for s in DeliveryPointStorage.objects.filter(LEGAL_CARGO_FILTER).values(
            "delivery_point_id",
            "delivery_point__name",
            "delivery_point__type",
            "cargo_key",
            "kind",
            "amount",
            "capacity",
        )
    ]

    # Group rows per DP. Sector fill is computed over the DP's INPUT rows in
    # this sector (respecting cargo/type exemptions); the storages payload
    # keeps only rows whose cargo is relevant to this sector, so e.g. a
    # construction site under metal shows just its H-Beam storage.
    sites: dict[int, dict] = {}
    from amc.economy_weights import row_in_sector

    for s in storages:
        s["capacity"] = await effective_capacity(s["cargo"], s["capacity"])
        in_sector = s["kind"] == "IN" and (
            sector == "other" or row_in_sector(sector, s["cargo"], s["type"])
        )
        relevant = sector == "other" or s["cargo"] in sector_cargo
        if not relevant:
            continue
        site = sites.setdefault(
            s["dp_id"],
            {
                "name": s["name"],
                "type": s["type"],
                "storages": [],
                "_amt": 0,
                "_cap": 0,
                "_in_sector": False,
                "starved": False,
            },
        )
        site["storages"].append(
            {
                "cargo": s["cargo"],
                "kind": s["kind"],
                "amount": s["amount"],
                "capacity": s["capacity"],
            }
        )
        if in_sector:
            site["_in_sector"] = True
            denom = s["capacity"] if s["capacity"] and s["capacity"] > 0 else 0
            site["_amt"] += s["amount"]
            site["_cap"] += denom
            site.setdefault("_rows", []).append((s["cargo"], s["amount"], denom))

    out = []
    out_kind = DeliveryPointStorage.Kind.OUTPUT
    for dp_id, site in sites.items():
        # starved = some sector INPUT row ≤15% with no same-cargo OUTPUT
        # stock to fall back on (warehouses keep their supply on the OUT side)
        site["starved"] = any(
            cap
            and amt / cap <= 0.15
            and not any(
                r["cargo"] == cargo and r["kind"] == out_kind and r["amount"] > 0
                for r in site["storages"]
            )
            for cargo, amt, cap in site.pop("_rows", [])
        )
        # only sites that actually have INPUT rows in this sector
        if not site.pop("_in_sector"):
            continue
        # exclude unmetered anonymous sites (all sector INPUT rows capacity-
        # less AND no type): the game spawns temporary DPs like "Building
        # Construction Site" for player-home construction — not permanent
        # economy sites. Typed sites (Warehouse, Factory, …) with unmetered
        # stock (e.g. food pallets at warehouses) stay listed, fill "–".
        if not site["_cap"] and not (site["type"] or "").strip():
            continue
        if starved_only and not site["starved"]:
            continue
        fill = site["_amt"] / site["_cap"] if site["_cap"] else None
        site["fill"] = round(fill, 4) if fill is not None else None
        site["storages"].sort(
            key=lambda r: (
                0 if r["kind"] == "IN" else 1,
                (r["amount"] / r["capacity"]) if r["capacity"] else 2,
            )
        )
        out.append(
            {
                "guid": dp_id,
                "name": site["name"],
                "type": site["type"],
                "fill": site["fill"],
                "starved": site["starved"],
                "storages": site["storages"],
            }
        )

    out.sort(
        key=lambda s: (not s["starved"], s["fill"] if s["fill"] is not None else 2)
    )
    total_cap = sum(s["_cap"] for s in sites.values())
    overall = (
        round(sum(s["_amt"] for s in sites.values()) / total_cap, 4)
        if total_cap
        else None
    )
    return {"sector": sector, "fill": overall, "sites": out[:limit]}
