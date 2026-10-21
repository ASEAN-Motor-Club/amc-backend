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
from typing import Any, Optional

logger = logging.getLogger("amc.delivery_telemetry")

# Snapshot is usable for enrichment while younger than this.
SNAPSHOT_MAX_AGE_SECONDS = 60 * 60  # 1 hour
# Minimum gap between background refreshes triggered by cache misses.
_BACKGROUND_REFRESH_DEBOUNCE_SECONDS = 60.0
_VEHICLE_FETCH_ATTEMPTS = 3

_snapshot_deliveries: dict[int, dict[str, Any]] = {}
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
    vehicle_deliveries: Optional[dict[int, dict[str, Any]]] = None,
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

    deliveries: dict[int, dict[str, Any]] = {}
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
                deliveries[entry["delivery_id"]] = entry

    # In-flight deliveries do NOT appear in `point.Net_Deliveries` (verified
    # empty on the dedicated server, 2026-10-21); accepted cargo lives on the
    # player's vehicle.  The caller passes vehicle-cargo entries so arriving
    # deliveries can be enriched — including the game's own computed payment
    # (`Net_Payment.BaseValue`) as the ground-truth expected pay.
    deliveries.update(vehicle_deliveries or {})

    _snapshot_deliveries = deliveries
    _snapshot_points = points_by_guid
    _snapshot_fetched_at = time.monotonic()
    return len(points_by_guid), len(deliveries)


async def fetch_vehicle_cargos(session) -> dict[int, dict[str, Any]]:
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
    deliveries: dict[int, dict[str, Any]] = {}
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
                deliveries.update(found)
                break
    logger.info("vehicle_cargos telemetry: %d entries", len(deliveries))
    return deliveries


async def _vehicle_cargos_for_player(
    session, character_guid: str
) -> dict[int, dict[str, Any]]:
    deliveries: dict[int, dict[str, Any]] = {}
    async with session.get(f"/players/{character_guid}/vehicle_cargos") as resp:
        if resp.status != 200:
            return {}
        payload = (await resp.json(content_type=None)).get("data") or []
    for vehicle in payload:
            if not isinstance(vehicle, dict):
                continue
            for space in vehicle.get("cargoSpaces") or []:
                if not isinstance(space, dict):
                    continue
                for cargo in space.get("cargos") or []:
                    entry = _vehicle_cargo_entry(cargo)
                    if entry is not None:
                        deliveries[entry["delivery_id"]] = entry
    return deliveries


def _vehicle_cargo_entry(cargo: Any) -> Optional[dict[str, Any]]:
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


def telemetry_for_delivery(delivery_id: Optional[int]) -> Optional[dict[str, Any]]:
    """Telemetry dict for a delivery id, or None (unknown / stale / no id)."""
    if not delivery_id or delivery_id <= 0:
        return None
    age = snapshot_age_seconds()
    if age is None or age > SNAPSHOT_MAX_AGE_SECONDS:
        return None
    entry = _snapshot_deliveries.get(delivery_id)
    if entry is None:
        return None
    result = dict(entry)
    receiver = _snapshot_points.get(entry.get("receiver_point") or "")
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