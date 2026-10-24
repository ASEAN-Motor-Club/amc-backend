"""Delivery telemetry: route/demand pricing context for cargo arrivals.

The MTDediMod Lua webserver exposes `GET /delivery/points` where each point
carries its pricing constants (`PaymentMultiplier`, `BasePayment`,
`DemandConfigs[].PaymentMultiplier`) and its registered deliveries
(`Net_Deliveries[]` with `PaymentMultiplierByDemand/BySupply/ByBalanceConfig`,
`PathDistance`, `PathClimbHeight`, `PathSpeedKPH`, `NumCargos`, `Weight`).
This is the data that decomposes a cargo payment into formula vs town bonus
vs demand pricing — the terms the backend previously could not observe
(2026-10-20 forensics: CocaPaste paying 7x more into one sink than another).

Design (behavior-neutral, fail-safe):

- A periodic worker job refreshes the snapshot into module memory.
- At cargo arrival, `handlers.cargo.process_cargo_log` enriches the log
  row's `data` JSON with `amc_delivery_telemetry` when the row's
  `Net_DeliveryId` is in the (fresh) snapshot.  A miss writes NOTHING —
  missing telemetry must never alter processing, only enrich logging.
- A background refresh is scheduled (debounced) when a looked-up delivery
  id is not in cache, so between polls new registrations get picked up.
- The game API being down is a soft failure: logged, never raised.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any, Optional

from asgiref.sync import sync_to_async

logger = logging.getLogger("amc.delivery_telemetry")

# Snapshot is usable for enrichment while younger than this.
SNAPSHOT_MAX_AGE_SECONDS = 60 * 60  # 1 hour
# Minimum gap between background refreshes triggered by cache misses.
_BACKGROUND_REFRESH_DEBOUNCE_SECONDS = 60.0
_VEHICLE_FETCH_ATTEMPTS = 3

_snapshot_deliveries: dict[int, list[dict[str, Any]]] = {}
_snapshot_points: dict[str, dict[str, Any]] = {}
_snapshot_fetched_at: Optional[float] = None
_refresh_lock: Optional[asyncio.Lock] = None
_last_background_refresh: float = 0.0


def _get_refresh_lock() -> asyncio.Lock:
    global _refresh_lock
    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()
    return _refresh_lock


def _delivery_entry(delivery: Any) -> Optional[dict[str, Any]]:
    """Reduce a Net_Delivery to the telemetry fields we keep."""
    try:
        delivery_id = int(delivery.get("ID") or 0)
    except (TypeError, ValueError):
        return None
    if delivery_id <= 0:
        return None
    return {
        "delivery_id": delivery_id,
        "cargo_key": delivery.get("CargoKey"),
        "num_cargos": delivery.get("NumCargos"),
        "weight": delivery.get("Weight"),
        "payment_multiplier_by_demand": delivery.get("PaymentMultiplierByDemand"),
        "payment_multiplier_by_supply": delivery.get("PaymentMultiplierBySupply"),
        "payment_multiplier_by_balance_config": delivery.get(
            "PaymentMultiplierByBalanceConfig"
        ),
        "path_distance": delivery.get("PathDistance"),
        "path_climb_height": delivery.get("PathClimbHeight"),
        "path_speed_kph": delivery.get("PathSpeedKPH"),
        "timer_seconds": delivery.get("TimerSeconds"),
        "delivery_flags": delivery.get("DeliveryFlags"),
        "sender_point": delivery.get("SenderPoint"),
        "receiver_point": delivery.get("ReceiverPoint"),
        "registered_time_seconds": delivery.get("RegisteredTimeSeconds"),
    }


def apply_snapshot(
    payload: dict[str, Any],
    vehicle_deliveries: Optional[dict[int, list[dict[str, Any]]]] = None,
) -> tuple[int, int]:
    """Ingest a `/delivery/points` response into the in-memory snapshot.

    `vehicle_deliveries` optionally carries in-flight cargo collected from
    online players' vehicles (see fetch_vehicle_cargos).  Returns
    (num_points, num_deliveries).  A malformed response raises — callers
    decide whether that is fatal (tests) or soft (worker cron).
    """
    global _snapshot_deliveries, _snapshot_points, _snapshot_fetched_at

    points = payload.get("data")
    if not isinstance(points, list):
        raise ValueError("delivery points response missing 'data' list")

    deliveries: dict[int, list[dict[str, Any]]] = {}
    points_by_guid: dict[str, dict[str, Any]] = {}
    for point in points:
        if not isinstance(point, dict):
            continue
        guid = point.get("DeliveryPointGuid")
        if guid:
            points_by_guid[guid] = {
                "payment_multiplier": point.get("PaymentMultiplier"),
                "base_payment": point.get("BasePayment"),
                "demand_configs": point.get("DemandConfigs") or [],
            }
        for delivery in point.get("Net_Deliveries") or []:
            entry = _delivery_entry(delivery)
            if entry is not None:
                deliveries.setdefault(entry["delivery_id"], []).append(entry)

    # In-flight deliveries do NOT appear in `point.Net_Deliveries` (verified
    # empty on the dedicated server, 2026-10-21); accepted cargo lives on the
    # player's vehicle.  The caller passes vehicle-cargo entries so arriving
    # deliveries can be enriched — including the game's own computed payment
    # (`Net_Payment.BaseValue`) as the ground-truth expected pay.
    for delivery_id, entries in (vehicle_deliveries or {}).items():
        if isinstance(entries, dict):  # tolerate a single entry per id
            entries = [entries]
        deliveries.setdefault(delivery_id, []).extend(entries)

    _snapshot_deliveries = deliveries
    _snapshot_points = points_by_guid
    _snapshot_fetched_at = time.monotonic()
    return len(points_by_guid), len(deliveries)


async def fetch_vehicle_cargos(session) -> dict[int, list[dict[str, Any]]]:
    """Collect accepted (in-flight) cargo from every online player's vehicle.

    The mod's vehicle_cargos endpoint intermittently returns empty payloads
    for a player (observed on prod 2026-10-21: the same in-flight delivery
    appears in one snapshot and is missing from the next).  Because a miss
    silently disables enrichment for that cycle, each player is retried and
    failures are isolated per player so one bad response can't blank the
    whole snapshot.
    """
    players = []
    for attempt in range(_VEHICLE_FETCH_ATTEMPTS):
        try:
            async with session.get("/players") as resp:
                if resp.status != 200:
                    logger.warning("/players returned %s", resp.status)
                    continue
                players = (await resp.json(content_type=None)).get("data") or []
                break
        except Exception:  # noqa: BLE001 — soft failure by design
            logger.warning("/players fetch failed attempt=%s", attempt, exc_info=True)
    deliveries: dict[int, list[dict[str, Any]]] = {}
    for player in players:
        if not isinstance(player, dict):
            continue
        character_guid = player.get("CharacterGuid")
        if not character_guid:
            continue
        for attempt in range(_VEHICLE_FETCH_ATTEMPTS):
            try:
                found = await _vehicle_cargos_for_player(session, character_guid)
            except Exception:  # noqa: BLE001 — soft failure by design
                logger.warning(
                    "vehicle_cargos fetch failed player=%s attempt=%s",
                    character_guid,
                    attempt,
                    exc_info=True,
                )
                continue
            if found:
                for delivery_id, entries in found.items():
                    deliveries.setdefault(delivery_id, []).extend(entries)
                break
    logger.info("vehicle_cargos telemetry: %d entries", len(deliveries))
    return deliveries


async def _vehicle_cargos_for_player(
    session, character_guid: str
) -> dict[int, list[dict[str, Any]]]:
    deliveries: dict[int, list[dict[str, Any]]] = {}
    async with session.get(f"/players/{character_guid}/vehicle_cargos") as resp:
        if resp.status != 200:
            return {}
        payload = (await resp.json(content_type=None)).get("data") or []
    for vehicle in payload:
        if not isinstance(vehicle, dict):
            continue
        # fullName looks like "Golima_Semi_C /Game/Maps/...:...Elisa2_C_21469..."
        # — the class token (first space-separated chunk) identifies the
        # vehicle (tractor) or trailer (Conter_Rear_40ft_C, Taber_Lead_C, ...).
        vehicle_name = str(vehicle.get("fullName") or "").split(" ", 1)[0]
        for space in vehicle.get("cargoSpaces") or []:
            if not isinstance(space, dict):
                continue
            for cargo in space.get("cargos") or []:
                entry = _vehicle_cargo_entry(cargo, vehicle_name)
                if entry is not None:
                    # Carried cargo is attributed to the character whose
                    # vehicle it sits in, so the reconnect quote cache can
                    # match an arrival that lost its Net_DeliveryId.
                    entry["character_guid"] = character_guid
                    # A player can haul SEVERAL batches of the same cargo to
                    # the same sink in one vehicle/trailer chain.  Those
                    # batches share Net_DeliveryId but were registered at
                    # different pickup refreshes, so each carries its own
                    # price.  Keep every entry — overwriting made the last
                    # stack's quote judge every arrival row of that id
                    # (2026-10-22 Bitzer: 17 coal units paid 1,920 were
                    # clawed against the other batch's 1,523 quote).
                    deliveries.setdefault(entry["delivery_id"], []).append(entry)
    return deliveries


def _vehicle_cargo_entry(cargo: Any, vehicle: str = "") -> Optional[dict[str, Any]]:
    """Reduce a vehicle-cargo actor to telemetry fields (per unit)."""
    if not isinstance(cargo, dict):
        return None
    try:
        delivery_id = int(cargo.get("Net_DeliveryId") or 0)
    except (TypeError, ValueError):
        return None
    if delivery_id <= 0:
        return None
    payment = cargo.get("Net_Payment") or {}
    sender = cargo.get("Net_SenderAbsoluteLocation") or {}
    destination = cargo.get("Net_DestinationLocation") or {}
    return {
        "delivery_id": delivery_id,
        "cargo_key": cargo.get("Net_CargoKey"),
        "vehicle": vehicle,
        "weight": cargo.get("Net_Weight"),
        "game_payment_base": payment.get("BaseValue"),
        "game_payment_shadowed": payment.get("ShadowedValue"),
        "empty_container_mass": cargo.get("EmptyContainerMass"),
        "sender_location": {
            k: sender.get(k) for k in ("X", "Y", "Z")
        },
        "destination_location": {
            k: destination.get(k) for k in ("X", "Y", "Z")
        },
        "source": "vehicle_cargos",
    }


def snapshot_age_seconds() -> Optional[float]:
    if _snapshot_fetched_at is None:
        return None
    return time.monotonic() - _snapshot_fetched_at


def _weights_match(a: Any, b: float) -> bool:
    """True when two per-unit weights are the same stack's weight.

    Tolerance covers float noise and the mod's rounding; containers report
    per-unit weights that can differ slightly between units of one stack.
    """
    try:
        wa = float(a)
    except (TypeError, ValueError):
        return False
    return abs(wa - b) <= max(0.5, 0.001 * abs(b))


def telemetry_for_delivery(
    delivery_id: Optional[int],
    cargo_key: Optional[str] = None,
    weight: Any = None,
) -> Optional[dict[str, Any]]:
    """Telemetry for a delivery id, scoped to the cargo and stack that arrived.

    A delivery id can legitimately cover several batches of the same cargo
    (pickup-spot refreshes re-price the same route), so the snapshot holds a
    LIST per id.  Candidates are filtered by cargo key first — a Corn quote
    must never excuse a Pumpkin payout — then by weight.  When more than one
    candidate survives, the highest quote is the enforcement value
    (`game_payment_base`) and every distinct quote is reported in
    `game_payment_bases` so all legitimate batches pass.

    Fail-safe: no candidate after filtering returns None, never a quote
    belonging to another cargo or another stack.
    """
    if not delivery_id or delivery_id <= 0:
        return None
    age = snapshot_age_seconds()
    if age is None or age > SNAPSHOT_MAX_AGE_SECONDS:
        return None
    entries = _snapshot_deliveries.get(delivery_id)
    if not entries:
        return None

    # 1. scope by cargo — different cargoes must never share a quote
    if cargo_key:
        entries = [e for e in entries if e.get("cargo_key") in (None, cargo_key)]
        if not entries:
            return None

    # 2. prefer the stack whose weight matches the arrived cargo; fall back
    #    to all cargo matches when nothing does (per-unit weight variance,
    #    float noise) so coverage is never silently lost.
    if weight is not None:
        try:
            row_weight = float(weight)
        except (TypeError, ValueError):
            row_weight = None
        if row_weight is not None:
            matched = [e for e in entries if _weights_match(e.get("weight"), row_weight)]
            if matched:
                entries = matched

    quotes: list[int] = []
    for entry in entries:
        base = entry.get("game_payment_base")
        if isinstance(base, (int, float)) and base > 0:
            quotes.append(int(base))
    distinct = sorted(set(quotes))

    # An entry with no computed price (board registrations) still enriches the
    # row with its multiplier/path context; it simply carries no enforcement
    # value, so the caller falls through to its other checks.
    primary = max(entries, key=lambda e: e.get("game_payment_base") or 0)
    result = dict(primary)
    if not distinct:
        receiver = _snapshot_points.get(result.get("receiver_point") or "")
        if receiver:
            result["receiver_payment_multiplier"] = receiver["payment_multiplier"]
            result["receiver_base_payment"] = receiver["base_payment"]
            result["receiver_demand_configs"] = receiver["demand_configs"]
        result["snapshot_age_seconds"] = round(snapshot_age_seconds() or 0, 1)
        return result

    # Highest quote wins enforcement: under-clawing is acceptable, never
    # compare a batch against another batch's lower registration.
    result["game_payment_base"] = distinct[-1]
    if len(distinct) > 1:
        result["game_payment_bases"] = distinct
        result["multi_quote"] = True
        logger.info(
            "Multi-quote delivery %s (%s): quotes=%s weights=%s vehicles=%s",
            delivery_id,
            cargo_key,
            distinct,
            sorted({e.get("weight") for e in entries}, key=str),
            sorted({e.get("vehicle") for e in entries}, key=str),
        )
    receiver = _snapshot_points.get(result.get("receiver_point") or "")
    if receiver:
        result["receiver_payment_multiplier"] = receiver["payment_multiplier"]
        result["receiver_base_payment"] = receiver["base_payment"]
        result["receiver_demand_configs"] = receiver["demand_configs"]
    result["snapshot_age_seconds"] = round(snapshot_age_seconds() or 0, 1)
    return result


async def refresh_snapshot(session) -> tuple[int, int]:
    """Fetch `/delivery/points` (field-filtered) from the mod server."""
    params = {"filters": "Net_Deliveries,PaymentMultiplier,BasePayment,DemandConfigs"}
    async with session.get("/delivery/points", params=params) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(
                f"/delivery/points returned {resp.status}: {body[:200]}"
            )
        payload = await resp.json(content_type=None)
    # Vehicle-cargo polling is soft-fail: its own errors never block the
    # board snapshot (the board source still enriches generated offers).
    try:
        vehicle_deliveries = await fetch_vehicle_cargos(session)
    except Exception:  # noqa: BLE001 — soft failure by design
        logger.warning("vehicle_cargos telemetry fetch failed", exc_info=True)
        vehicle_deliveries = {}
    # Persist the registrations so an arrival that loses its delivery id
    # (disconnect/reconnect mid-haul) can still be priced from the game's
    # own BaseValue instead of stale route history.
    await sync_to_async(record_quote_cache)(vehicle_deliveries)
    return apply_snapshot(payload, vehicle_deliveries)


async def refresh_background(session) -> None:
    """Debounced background refresh for cache misses (soft-fail)."""
    global _last_background_refresh
    now = time.monotonic()
    if now - _last_background_refresh < _BACKGROUND_REFRESH_DEBOUNCE_SECONDS:
        return
    _last_background_refresh = now
    lock = _get_refresh_lock()
    if lock.locked():
        return
    async with lock:
        try:
            points, deliveries = await refresh_snapshot(session)
            logger.info(
                "Delivery telemetry snapshot refreshed: %d points, %d deliveries",
                points,
                deliveries,
            )
        except Exception as exc:  # noqa: BLE001 — soft failure by design
            logger.warning("Delivery telemetry refresh failed: %s", exc)


async def schedule_refresh_on_miss(session, delivery_id: Optional[int]) -> None:
    """Fire-and-forget background refresh when a delivery id is unknown."""
    if not delivery_id:
        return
    age = snapshot_age_seconds()
    if age is not None and age < _BACKGROUND_REFRESH_DEBOUNCE_SECONDS:
        return  # snapshot is fresh; the delivery simply is not on the board
    asyncio.create_task(refresh_background(session))


def unknown_cargo_marker(cargo_key: str) -> Optional[bool]:
    """True when the cargo key has NO definition row (gamedata gap).

    Used by the cargo handler to tag log rows with `amc_unknown_cargo` so
    unknown cargos surface in audits and get added to cargo_definitions.
    Imported lazily to avoid an import cycle at module load.
    """
    from amc.cargo_definitions import cargo_is_known

    if cargo_is_known(cargo_key):
        return None
    return True


# --- Reconnect quote cache -------------------------------------------------
# The game resets Net_DeliveryId to 0 when a player disconnects mid-haul, so
# the arriving row has no id to match against the snapshot and falls back to
# route history (which is stale and claws legitimate hauls).  Every tick we
# persist the registrations seen on players' vehicles; an unregistered
# arrival is then matched back to its own registration by character + cargo
# + weight + pickup/destination.  Single-player cargo has no registration,
# so it never matches and still falls through to history.
QUOTE_CACHE_MAX_AGE = timedelta(hours=12)
QUOTE_CACHE_RETENTION = timedelta(days=7)
QUOTE_CACHE_MAX_CANDIDATES = 100
# Same route means the same delivery-point coordinate (the arrival resolves
# its point within a 1 m buffer), so this only absorbs float noise.
QUOTE_CACHE_ROUTE_TOLERANCE_M = 50.0


def record_quote_cache(deliveries: dict[int, list[dict[str, Any]]]) -> int:
    """Persist in-flight registrations so a reconnect can still be priced.

    Sync ORM by design: called from async code through
    ``asgiref.sync.sync_to_async``.  Soft-fail — any database error is
    logged and swallowed, because this cache only improves a fallback and
    must never block the snapshot.
    """
    try:
        from django.utils import timezone

        from amc.models import Character, DeliveryQuoteCache

        now = timezone.now()
        guids = {
            str(entry["character_guid"])
            for entries in deliveries.values()
            for entry in entries
            if entry.get("character_guid")
        }
        if not guids:
            return 0
        character_ids: dict[str, int] = {}
        lookup_guids = {g.upper() for g in guids} | {g.lower() for g in guids}
        for row in Character.objects.filter(guid__in=lookup_guids).values(
            "id", "guid"
        ):
            character_ids[(row["guid"] or "").upper()] = row["id"]

        rows: dict[tuple, DeliveryQuoteCache] = {}
        for entries in deliveries.values():
            for entry in entries:
                character_id = character_ids.get(
                    (entry.get("character_guid") or "").upper()
                )
                base_value = entry.get("game_payment_base")
                if character_id is None or not base_value:
                    continue
                sender = entry.get("sender_location") or {}
                destination = entry.get("destination_location") or {}
                key = (
                    character_id,
                    entry["delivery_id"],
                    entry.get("cargo_key") or "",
                    entry.get("weight"),
                )
                # A vehicle hauling N units of one cargo reports N cargo
                # entries with the SAME key.  Postgres rejects an
                # ON CONFLICT DO UPDATE that touches one row twice in a
                # single statement, so collapse them here and keep the
                # highest quote (enforcement uses the highest anyway).
                existing = rows.get(key)
                if existing is not None and (existing.base_value or 0) >= base_value:
                    continue
                rows[key] = DeliveryQuoteCache(
                    character_id=character_id,
                    delivery_id=entry["delivery_id"],
                    cargo_key=entry.get("cargo_key") or "",
                    weight=entry.get("weight"),
                    vehicle=entry.get("vehicle"),
                    base_value=base_value,
                    sender_x=sender.get("X"),
                    sender_y=sender.get("Y"),
                    sender_z=sender.get("Z"),
                    dest_x=destination.get("X"),
                    dest_y=destination.get("Y"),
                    dest_z=destination.get("Z"),
                    first_seen=now,
                    last_seen=now,
                )
        if not rows:
            return 0
        DeliveryQuoteCache.objects.bulk_create(
            list(rows.values()),
            update_conflicts=True,
            unique_fields=["character", "delivery_id", "cargo_key", "weight"],
            update_fields=["base_value", "vehicle", "last_seen"],
        )
        DeliveryQuoteCache.objects.filter(
            last_seen__lt=now - QUOTE_CACHE_RETENTION
        ).delete()
        return len(rows)
    except Exception:  # noqa: BLE001 — cache is best-effort
        # ERROR, not WARNING: the ``amc`` logger is configured at ERROR, so a
        # warning here is invisible — a silently failing write is exactly how
        # the 2026-10-24 duplicate-key bug went unnoticed for hours.
        logger.error("quote cache write failed", exc_info=True)
        return 0


def _cache_location_matches(point, x: Any, y: Any, z: Any) -> Optional[bool]:
    """True/False when both sides are known, None when the check is moot.

    A delivery point's ``coord`` is the point itself (the arrival handler
    resolves it by requiring the point within a 1 m buffer of the cargo's
    sender/destination location), so the same route means the same
    coordinates.  The tolerance only absorbs float noise.
    """
    if point is None or x is None or y is None:
        return None
    try:
        from django.contrib.gis.geos import Point
    except Exception:  # noqa: BLE001 — GIS unavailable
        return None
    try:
        cached = Point(float(x), float(y), float(z or 0.0))
        return point.coord.distance(cached) <= QUOTE_CACHE_ROUTE_TOLERANCE_M
    except Exception:  # noqa: BLE001 — malformed geometry
        return None


def lookup_quote_cache(
    character_id: Optional[int],
    cargo_key: Optional[str],
    weight: Any,
    sender_point=None,
    destination_point=None,
) -> Optional[dict[str, Any]]:
    """Recover the game's own price for an arrival that lost its id.

    Sync ORM by design (called through ``sync_to_async``).  Returns a
    telemetry-shaped dict (source ``quote_cache``) so downstream validation
    consumes it exactly like a live snapshot quote, or None when nothing
    matches — never a guess from another cargo or another route.
    """
    if not character_id or not cargo_key or weight is None:
        return None
    try:
        from django.utils import timezone

        from amc.models import DeliveryQuoteCache

        now = timezone.now()
        rows = list(
            DeliveryQuoteCache.objects.filter(
                character_id=character_id,
                cargo_key=cargo_key,
                base_value__gt=0,
                last_seen__gte=now - QUOTE_CACHE_MAX_AGE,
            ).order_by("-last_seen")[:QUOTE_CACHE_MAX_CANDIDATES]
        )
    except Exception:  # noqa: BLE001 — lookup is best-effort
        logger.error("quote cache lookup failed", exc_info=True)
        return None

    try:
        wanted_weight = float(weight)
    except (TypeError, ValueError):
        return None

    best = None
    for row in rows:
        if not _weights_match(row.weight, wanted_weight):
            continue
        sender_ok = _cache_location_matches(
            sender_point, row.sender_x, row.sender_y, row.sender_z
        )
        dest_ok = _cache_location_matches(
            destination_point, row.dest_x, row.dest_y, row.dest_z
        )
        if sender_ok is False or dest_ok is False:
            continue
        # Several batches can share one route; enforce only against the
        # highest cached quote so a legitimate batch is never clawed.
        if best is None or (row.base_value or 0) > (best.base_value or 0):
            best = row
    if best is None:
        return None
    return {
        "source": "quote_cache",
        "delivery_id": best.delivery_id,
        "game_payment_base": best.base_value,
        "weight": best.weight,
        "vehicle": best.vehicle,
        "cache_age_seconds": round((now - best.last_seen).total_seconds(), 1),
        "quote_cache": True,
    }