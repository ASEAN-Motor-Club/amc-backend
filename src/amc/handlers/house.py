"""House event handlers.

Handles: ServerRentExtendHouse
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db.models import F, Sum
from django.utils import timezone

from amc.handlers import register
from amc.mod_server import transfer_money
from amc.models import Delivery
from amc_finance.services import record_treasury_rent_income, send_fund_to_player_wallet

logger = logging.getLogger("amc.webhook.handlers.house")


@register("ServerRentExtendHouse")
async def handle_rent_extend(event, player, character, ctx):
    data = event["data"]
    rent_cost = int(data.get("Money", 0))
    if rent_cost <= 0:
        return 0, 0, 0, 0

    await record_treasury_rent_income(rent_cost, f"House Rent — {character.guid}")

    cutoff = timezone.now() - timezone.timedelta(days=settings.RENT_REBATE_LOOKBACK_DAYS)
    total_earnings = (
        await Delivery.objects.filter(character=character, timestamp__gte=cutoff).aaggregate(
            total=Sum(F("payment") + F("subsidy"))
        )
    )["total"] or 0

    rebate = min(total_earnings, rent_cost)
    if rebate > 0 and ctx.http_client_mod:
        try:
            await transfer_money(
                ctx.http_client_mod,
                rebate,
                "House Rent Rebate",
                str(character.player.unique_id),
            )
            await send_fund_to_player_wallet(rebate, character, "House Rent Rebate")
        except Exception:
            logger.warning(
                "Failed to send rent rebate of %d to %s", rebate, character.guid, exc_info=True
            )

    return 0, 0, 0, 0
