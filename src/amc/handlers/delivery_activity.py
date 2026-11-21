"""Delivery activity tracking — who accepted / picked up a delivery.

Handles: ServerAcceptDelivery

``ServerPickupCargo`` is registered by ``handlers/police.py`` (the registry
holds one handler per hook); that handler calls :func:`record_cargo_pickup`
first so pickups land in the same log as accepts.

Why this exists: an AMC ``DeliveryJob`` is only credited at cargo ARRIVAL,
where attribution is a route + cargo match.  That means the backend learns
who worked a job after the fact, and never learns who accepted a delivery
and then abandoned it.  This log records the two earlier game signals —
``ServerAcceptDelivery`` (player took a delivery off the board) and
``ServerPickupCargo`` (the cargo actor was actually picked up) — keyed by the
game ``delivery_id``, which is the same id stored on
``ServerCargoArrivedLog.delivery_id``.  Accept → pickup → arrival can then be
reconstructed per character.

Tracking is strictly observational: every entry point is wrapped so a failure
can never abort payment processing or surface the "Webhook failed" popup.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Iterable, Optional

from django.utils import timezone

from amc import delivery_telemetry
from amc.handlers import register
from amc.handlers.utils import parse_event_timestamp
from amc.models import DeliveryActivityLog, DeliveryJob, DeliveryPoint

logger = logging.getLogger("amc.webhook.handlers.delivery_activity")

ZERO_PAYMENT = (0, 0, 0, 0)

# Mirrors DeliveryActivityLog.Kind — kept as plain strings so the handler does
# not depend on Django's TextChoices descriptor at call time.
KIND_ACCEPTED = "accepted"
KIND_PICKED_UP = "picked_up"

# The accept RPC can be retried by the client (bShowErrorMessage) and a player
# can re-open the board; a replay inside this window is not new activity.
ACCEPT_DEDUPE_WINDOW_HOURS = 12


async def resolve_points(*guids: Optional[str]) -> list[Optional[DeliveryPoint]]:
    """Map game delivery-point guids (any case) to DeliveryPoint rows."""
    wanted = {str(g).upper() for g in guids if g}
    if not wanted:
        return [None for _ in guids]
    rows = {
        (dp.guid or "").upper(): dp
        async for dp in DeliveryPoint.objects.filter(
            guid__in=[*wanted, *(g.lower() for g in wanted)]
        )
    }
    return [rows.get(str(g).upper()) if g else None for g in guids]


async def match_job(
    cargo_key: Optional[str],
    sender: Optional[DeliveryPoint],
    destination: Optional[DeliveryPoint],
    character: Any,
) -> Optional[DeliveryJob]:
    """Active AMC job the delivery would credit, when the route is known.

    Attribution needs the sender AND destination points: ``filter_by_delivery``
    treats a NULL job-side point as "any", so passing an unresolved point as
    NULL would match jobs for the wrong route.  Without both, leave the job
    empty — the arrival handler stamps it authoritatively.
    """
    if not cargo_key or sender is None or destination is None:
        return None
    job = await (
        DeliveryJob.objects.filter_active()
        .filter_by_delivery(sender, destination, cargo_key)
        .afirst()
    )
    if job is not None and job.rp_mode and not (character and character.rp_mode):
        return None
    return job


async def record_activity(
    *,
    kind: str,
    timestamp: Any,
    player: Any,
    character: Any,
    delivery_id: Optional[int],
    cargo_key: Optional[str] = None,
    payment: Optional[int] = None,
    weight: Optional[float] = None,
    data: Optional[dict[str, Any]] = None,
) -> Optional[DeliveryActivityLog]:
    """Write one activity row, enriching it from the live delivery snapshot."""
    if not delivery_id or delivery_id <= 0:
        return None

    telemetry = delivery_telemetry.telemetry_for_delivery(
        delivery_id,
        cargo_key=cargo_key,
        weight=weight,
        character_guid=str(character.guid) if character is not None else None,
    )
    sender = destination = None
    if telemetry:
        cargo_key = cargo_key or telemetry.get("cargo_key")
        sender, destination = await resolve_points(
            telemetry.get("sender_point"), telemetry.get("receiver_point")
        )
    job = await match_job(cargo_key, sender, destination, character)

    return await DeliveryActivityLog.objects.acreate(
        timestamp=timestamp,
        kind=kind,
        player=player,
        character=character,
        delivery_id=delivery_id,
        cargo_key=cargo_key,
        sender_point=sender,
        destination_point=destination,
        payment=payment,
        weight=weight,
        job=job,
        data={**(data or {}), "telemetry": telemetry},
    )


async def record_accept_delivery(event, player, character) -> None:
    """ServerAcceptDelivery: the player took a delivery job off the board."""
    payload = event.get("data") or {}
    try:
        delivery_id = int(payload.get("DeliveryId") or 0)
    except (TypeError, ValueError):
        return
    if delivery_id <= 0:
        return

    if character is not None:
        duplicate = await DeliveryActivityLog.objects.filter(
            character=character,
            delivery_id=delivery_id,
            kind=KIND_ACCEPTED,
            timestamp__gte=timezone.now() - timedelta(hours=ACCEPT_DEDUPE_WINDOW_HOURS),
        ).aexists()
        if duplicate:
            return

    await record_activity(
        kind=KIND_ACCEPTED,
        timestamp=parse_event_timestamp(event),
        player=player,
        character=character,
        delivery_id=delivery_id,
        data={"raw": payload},
    )


async def record_cargo_pickup(event, player, character) -> None:
    """ServerPickupCargo: a cargo actor for a delivery was picked up.

    Called from the police handler (confiscation), which owns the hook.  Fires
    once per cargo actor, so N units of one delivery produce N rows — that is
    the point (a pickup is per unit, an accept is per delivery).
    """
    cargo = (event.get("data") or {}).get("Cargo") or {}
    try:
        delivery_id = int(cargo.get("Net_DeliveryId") or 0)
    except (TypeError, ValueError):
        return
    if delivery_id <= 0:
        # Non-job cargo (free roaming / own goods) — nothing to track.
        return

    payment = cargo.get("Net_Payment")
    weight = cargo.get("Net_Weight")
    await record_activity(
        kind=KIND_PICKED_UP,
        timestamp=parse_event_timestamp(event),
        player=player,
        character=character,
        delivery_id=delivery_id,
        cargo_key=cargo.get("Net_CargoKey"),
        payment=int(payment) if isinstance(payment, (int, float)) else None,
        weight=float(weight) if isinstance(weight, (int, float)) else None,
        data={"raw": cargo},
    )


async def stamp_job_for_delivery_ids(
    delivery_ids: Iterable[Optional[int]], job_id: int
) -> int:
    """Attach the job an arrival was credited to onto that delivery's rows.

    The arrival handler is the authoritative attribution point (route + cargo
    match against live jobs), so its verdict overwrites anything guessed at
    accept/pickup time.  Fail-safe: tracking must never break a delivery.
    """
    ids = sorted({int(d) for d in delivery_ids if d and int(d) > 0})
    if not ids or not job_id:
        return 0
    try:
        return await DeliveryActivityLog.objects.filter(delivery_id__in=ids).aupdate(
            job_id=job_id
        )
    except Exception:  # noqa: BLE001 — observational only
        logger.warning(
            "Failed to stamp delivery activity with job %s", job_id, exc_info=True
        )
        return 0


@register("ServerAcceptDelivery")
async def handle_accept_delivery(event, player, character, ctx):
    try:
        await record_accept_delivery(event, player, character)
    except Exception:  # noqa: BLE001 — observational only
        logger.warning("Failed to record delivery accept", exc_info=True)
    return ZERO_PAYMENT
