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

from django.utils import timezone

from amc.economy_weights import sector_of, weight_of
from amc.models import Delivery, DeliveryPointStorage, StorageSnapshot

logger = logging.getLogger("amc.economy_dashboard")

MAX_HAUL_DEGREE = 1.0  # starvation weight when no snapshot/capacity data exists


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
        async for dp_id, kind, cargo_key, amount, capacity in DeliveryPointStorage.objects.values_list(
            "delivery_point_id", "kind", "cargo_key", "amount", "capacity"
        )
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
        (d["character_id"], d["cargo_key"], d["quantity"], d["payment"],
         d["destination_point_id"], int(d["timestamp"].timestamp()))
        async for d in Delivery.objects.filter(
            timestamp__range=[window_start, window_end],
            character__isnull=False,
        )
        .values("character_id", "cargo_key", "quantity", "payment",
                "destination_point_id", "timestamp")
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
            grouped.setdefault(key, []).append(
                (int(s[2].timestamp()), s[3], s[4], 0)
            )
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
    rows = [
        (r["cargo_key"], r["amount"], r["capacity"])
        async for r in DeliveryPointStorage.objects.filter(kind="IN", capacity__gt=0)
        .values("cargo_key", "amount", "capacity")
    ]
    by_sector: dict[str, dict] = {}
    for cargo_key, amount, capacity in rows:
        sector = sector_of(cargo_key)
        if sector == "other":
            continue
        s = by_sector.setdefault(sector, {"amount": 0, "capacity": 0, "starved": 0})
        s["amount"] += amount
        s["capacity"] += capacity
        if amount / capacity <= 0.15:
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
