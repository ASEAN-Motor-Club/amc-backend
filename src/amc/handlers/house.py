"""House event handlers.

Handles: ServerRentHouse, ServerRentExtendHouse
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db.models import F, Sum
from django.utils import timezone

from amc.handlers import register
from amc.mod_server import transfer_money
from amc.models import Delivery, HousingLicense
from amc_finance.services import record_treasury_rent_income, send_fund_to_player_wallet

logger = logging.getLogger("amc.webhook.handlers.house")


@register("ServerRentHouse")
async def handle_rent(event, player, character, ctx):
    logger.info(
        "ServerRentHouse: player=%s character=%s house=%s",
        player.unique_id,
        character.guid,
        event["data"].get("HouseGuid"),
    )
    return 0, 0, 0, 0


@register("ServerRentExtendHouse")
async def handle_rent_extend(event, player, character, ctx):
    data = event["data"]
    rent_cost = int(data.get("Money", 0))
    if rent_cost <= 0:
        return 0, 0, 0, 0

    await record_treasury_rent_income(rent_cost, f"House Rent — {character.guid}")

    seconds = float(data.get("Seconds", 0))
    if seconds > 0:
        lookback_days = (seconds / 86400) * 2
    else:
        lookback_days = settings.RENT_REBATE_LOOKBACK_DAYS * 2
    cutoff = timezone.now() - timezone.timedelta(days=lookback_days)

    total_earnings = (
        await Delivery.objects.filter(
            character=character, timestamp__gte=cutoff
        ).aaggregate(total=Sum(F("payment") + F("subsidy")))
    )["total"] or 0

    house_guid = data.get("HouseGuid")
    licenses = HousingLicense.objects.filter(character=character)
    exact = (
        licenses.filter(house_key=house_guid)
        if house_guid
        else HousingLicense.objects.none()
    )
    general = licenses.filter(house_key__isnull=True)
    license = await exact.order_by("-rebate_pct").afirst()
    if license is None:
        license = await general.order_by("-rebate_pct").afirst()

    if license:
        effective_cost = int(rent_cost * license.rebate_pct / 100)
    else:
        effective_cost = rent_cost

    rebate = min(total_earnings, effective_cost)
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
                "Failed to send rent rebate of %d to %s",
                rebate,
                character.guid,
                exc_info=True,
            )

    return 0, 0, 0, 0
