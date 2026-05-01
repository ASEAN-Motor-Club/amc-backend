"""Atomic delivery processing.

Extracted from webhook.py.  Handles the sync DB transaction for
creating Delivery records and updating DeliveryJob quantities.
"""

from __future__ import annotations

from typing import Any, cast

from django.db import transaction
from django.db.models import F

from amc.models import Delivery, DeliveryJob

from amc import config


def atomic_process_delivery(job_id, quantity, delivery_data):
    """Atomically update the job and create the delivery log."""
    with transaction.atomic():
        job = None
        quantity_to_add = 0
        weighted_quantity = quantity
        if job_id:
            job = DeliveryJob.objects.select_for_update().get(pk=job_id)
            requested_remaining = job.quantity_requested - job.quantity_fulfilled
            weight = config.CARGO_FULFILLMENT_WEIGHTS.get(
                delivery_data.get("cargo_key") or "", 1
            )
            weighted_quantity = quantity * weight
            quantity_to_add = min(requested_remaining, weighted_quantity)
            if quantity_to_add > 0:
                job.quantity_fulfilled = cast(
                    Any, F("quantity_fulfilled") + quantity_to_add
                )
                job.save(update_fields=["quantity_fulfilled"])
                job.refresh_from_db(fields=["quantity_fulfilled"])

        bonus = 0
        if job and quantity_to_add > 0:
            multiplier = max(0, job.bonus_multiplier)
            bonus = int(
                delivery_data["payment"]
                * (quantity_to_add / weighted_quantity)
                * multiplier
                + 0.5
            )
            delivery_data["subsidy"] += bonus

        Delivery.objects.create(job=job, **delivery_data)
        return job
