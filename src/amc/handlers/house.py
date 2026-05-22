"""House event handlers.

Handles: ServerRentHouse, ServerRentExtendHouse
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db.models import F, Sum
from django.utils import timezone

from amc.handlers import register
from amc.mod_server import get_houses, get_rent_info, show_popup, transfer_money
from amc.models import Delivery, House, HousingLicense
from amc_cogs.housing_market import get_market_multiplier
from amc_finance.services import record_treasury_rent_income, send_fund_to_player_wallet

logger = logging.getLogger("amc.webhook.handlers.house")

DEFAULT_HOUSING_RATIO = 5.0


async def _compute_rent_cost(http_client_mod, house_guid):
    """Compute the rent cost for a house from the mod's server config + House model."""
    if not house_guid:
        return 0

    try:
        rent_info = await get_rent_info(http_client_mod, house_guid)
    except Exception:
        logger.warning("Failed to get rent info for house %s", house_guid, exc_info=True)
        return 0

    ratio = rent_info.get("HousingPlotRentalPriceRatio", DEFAULT_HOUSING_RATIO)
    max_days = rent_info.get("MaxHousingPlotRentalDays", 15)
    house_key = rent_info.get("HousegKey", "")
    if not house_key:
        return 0

    try:
        house_obj = await House.objects.aget(key=house_key)
    except House.DoesNotExist:
        logger.warning("House model not found for key=%s (guid=%s)", house_key, house_guid)
        return 0

    base_cost = int(house_obj.cost * ratio)

    try:
        houses = await get_houses(http_client_mod)
        multiplier, _ = await get_market_multiplier(houses, max_days)
    except Exception:
        logger.warning("Failed to compute market multiplier, using 1.0", exc_info=True)
        multiplier = 1.0

    return int(base_cost * multiplier)


@register("ServerRentHouse")
async def handle_rent(event, player, character, ctx):
    logger.info(
        "ServerRentHouse: player=%s character=%s house=%s blocked=%s",
        player.unique_id,
        character.guid,
        event["data"].get("HouseGuid"),
        event["data"].get("Blocked"),
    )
    if event["data"].get("Blocked") and ctx.http_client_mod:
        house_guid = event["data"].get("HouseGuid")
        rent_cost = await _compute_rent_cost(ctx.http_client_mod, house_guid)

        if rent_cost > 0:
            try:
                await transfer_money(
                    ctx.http_client_mod,
                    rent_cost,
                    "House Rent Refund",
                    str(player.unique_id),
                )
                logger.info(
                    "Refunded rent of %d to player=%s character=%s house=%s",
                    rent_cost, player.unique_id, character.guid, house_guid,
                )
            except Exception:
                logger.warning(
                    "Failed to refund rent of %d to %s",
                    rent_cost, character.guid, exc_info=True,
                )

        try:
            await show_popup(
                ctx.http_client_mod,
                "House rentals must be done through Discord. Use /house buy in Discord. Your money has been refunded.",
                character_guid=str(character.guid),
            )
        except Exception:
            logger.warning("Failed to send rent-blocked popup to %s", character.guid, exc_info=True)
    return 0, 0, 0, 0


@register("ServerRentExtendHouse")
async def handle_rent_extend(event, player, character, ctx):
    data = event["data"]
    rent_cost = int(data.get("Money", 0))
    if rent_cost <= 0:
        return 0, 0, 0, 0

    if data.get("Blocked") and ctx.http_client_mod:
        try:
            await show_popup(
                ctx.http_client_mod,
                "House rent extensions must be done through Discord. Use /house extend in Discord.",
                character_guid=str(character.guid),
            )
        except Exception:
            logger.warning("Failed to send extend-blocked popup to %s", character.guid, exc_info=True)
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
