"""Tow request event handler.

Handles: ServerTowRequestArrived
"""

from __future__ import annotations

import logging

from amc.handlers import register
from amc.models import ServerTowRequestArrivedLog
from amc.fraud_detection import validate_tow_payment
from amc.pipeline.discord import post_discord_fraud_alert

logger = logging.getLogger("amc.webhook.handlers.tow")


@register("ServerTowRequestArrived")
async def handle_tow_request(event, player, character, ctx):
    timestamp = _parse_timestamp(event)
    tow_request = event["data"].get("TowRequest")
    tow_data = tow_request or {}
    payment = tow_data.get("Net_Payment", 0)

    # Body damage bonus
    body_damage = tow_data.get("BodyDamage", 1.0)
    payment += int(payment * 0.55 * (1 - body_damage))

    # Fraud detection
    fraud_excess = validate_tow_payment(payment)
    if fraud_excess > 0:
        logger.warning(
            "Fraud detected (tow): player=%s original=%d reduced=%d excess=%d",
            player.unique_id,
            payment,
            max(0, payment - fraud_excess),
            fraud_excess,
        )
        payment = max(0, payment - fraud_excess)
        post_discord_fraud_alert(
            ctx.discord_client,
            kind="tow_over_ceiling",
            character_name=character.name if character else "unknown",
            player_id=str(player.unique_id),
            original_payment=payment + fraud_excess,
            clawed_back=fraud_excess,
            final_payment=payment,
            detail="Tow request payment exceeded the ceiling.",
        )

    await ServerTowRequestArrivedLog.objects.acreate(
        timestamp=timestamp,
        player=player,
        payment=payment,
        data=tow_data,
    )

    match tow_data.get("Net_TowRequestFlags", 0):
        case 1:  # Flipped
            subsidy = 2_000 + payment * 1.0
        case _:
            subsidy = 2_000 + payment * 0.5

    # Contract: base_pay includes the clawback amount; process_events claws
    # fraud_excess from the wallet and subtracts it from the batch income.
    return payment + fraud_excess, subsidy, 0, fraud_excess


def _parse_timestamp(event):
    from amc.handlers.utils import parse_event_timestamp

    return parse_event_timestamp(event)
