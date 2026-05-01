"""Passenger event handler.

Handles: ServerPassengerArrived, ServerAcceptPassenger
"""

from __future__ import annotations

import asyncio
import logging

from amc.handlers import register
from amc.models import ServerPassengerArrivedLog
from amc.mod_server import show_popup, transfer_money
from amc.fraud_detection import validate_passenger_payment
from amc.subsidies import get_passenger_subsidy

logger = logging.getLogger("amc.webhook.handlers.passenger")


@register("ServerPassengerArrived")
async def handle_passenger_arrived(event, player, character, ctx):
    timestamp = _parse_timestamp(event)
    passenger = event["data"].get("Passenger")
    passenger_data = passenger or {}
    base_payment = passenger_data.get("Net_Payment", 0)
    flag = passenger_data.get("Net_PassengerFlags", 0)

    if base_payment < 0:
        raise ValueError(f"Negative payment for passenger: {passenger_data}")

    # Exploit detection: passengers picked up on a modded server have
    # Net_StartLocation at the world origin (0,0,0).
    start_loc = passenger_data.get("Net_StartLocation", {})
    is_exploit = (
        start_loc.get("X", 1) == 0
        and start_loc.get("Y", 1) == 0
        and start_loc.get("Z", 1) == 0
    )

    log = ServerPassengerArrivedLog(
        timestamp=timestamp,
        player=player,
        passenger_type=passenger_data.get("Net_PassengerType", ""),
        distance=passenger_data.get("Net_Distance"),
        payment=base_payment,
        arrived=passenger_data.get("Net_bArrived", True),
        comfort=bool(flag & 1),
        urgent=bool(flag & 2),
        limo=bool(flag & 4),
        offroad=bool(flag & 8),
        comfort_rating=passenger_data.get("Net_LCComfortSatisfaction"),
        urgent_rating=passenger_data.get("Net_TimeLimitPoint"),
        data=passenger_data,
    )

    if is_exploit:
        log.payment = 0
        await log.asave()
        if base_payment > 0 and character and ctx.http_client_mod:
            await transfer_money(
                ctx.http_client_mod,
                int(-base_payment),
                "Invalid Passenger",
                str(character.player.unique_id),
            )
            asyncio.create_task(
                show_popup(
                    ctx.http_client_mod,
                    "Passenger delivery rejected: invalid origin.",
                    character_guid=character.guid,
                    player_id=str(character.player.unique_id),
                )
            )
        logger.warning(
            "Exploit detected: passenger with zero start location for player %s (payment=%s)",
            player.unique_id,
            base_payment,
        )
        return 0, 0, 0, 0

    # Fraud detection: validate payment against type ceiling
    passenger_type_int = int(log.passenger_type) if log.passenger_type else 0
    fraud_excess = validate_passenger_payment(passenger_type_int, base_payment)
    if fraud_excess > 0:
        original = base_payment
        base_payment = max(0, base_payment - fraud_excess)
        log.payment = base_payment
        logger.warning(
            "Fraud detected (passenger): player=%s type=%s original=%d reduced=%d excess=%d",
            player.unique_id,
            passenger_type_int,
            original,
            base_payment,
            fraud_excess,
        )

    # Taxi bonuses
    if log.passenger_type == ServerPassengerArrivedLog.PassengerType.Taxi:
        if log.comfort:
            bonus_per_star = 0.2
            if log.limo:
                bonus_per_star = bonus_per_star * 1.3
            log.payment += base_payment * log.comfort_rating * bonus_per_star
        if log.urgent:
            log.payment += base_payment * log.urgent_rating * 0.3

    # Ambulance bonus
    if log.passenger_type == ServerPassengerArrivedLog.PassengerType.Ambulance:
        radius_ratio = passenger_data.get("Net_SearchAndRescueRadiusRatio")
        if radius_ratio is not None:
            bonus_multiplier = 1 - radius_ratio
            log.payment += int(base_payment * bonus_multiplier)

    await log.asave()

    from amc.guilds import check_guild_passenger

    session, bonus = await check_guild_passenger(
        character, log.passenger_type, log.comfort, log.urgent,
        log.limo, log.offroad, log.comfort_rating, log.payment,
    )
    if session:
        log.guild_session = session
        log.payment += bonus
        await log.asave(update_fields=["guild_session", "payment"])

        from amc.guilds import check_guild_achievements
        await check_guild_achievements(character, session, log, ctx.http_client_mod)

    subsidy = get_passenger_subsidy(log)
    return log.payment, subsidy, 0, 0


@register("ServerAcceptPassenger")
async def handle_accept_passenger(event, player, character, ctx):
    if not character:
        return 0, 0, 0, 0

    from amc.models import GuildSession

    session = await (
        GuildSession.objects.filter(character=character, ended_at__isnull=True)
        .select_related("guild__passenger_requirement")
        .afirst()
    )
    if not session:
        return 0, 0, 0, 0

    try:
        req = session.guild.passenger_requirement
    except Exception:
        return 0, 0, 0, 0

    type_labels = {
        0: "Any",
        1: "Hitchhiker",
        2: "Taxi",
        3: "Ambulance",
        4: "Bus",
    }
    allowed = req.allowed_passenger_types or []
    parts = []
    if allowed:
        labels = [type_labels.get(t, str(t)) for t in allowed]
        parts.append(f"Types: {', '.join(labels)}")
    if req.require_comfort:
        parts.append("Comfort required")
    if req.require_urgent:
        parts.append("Urgent required")
    if req.require_limo:
        parts.append("Limo required")
    if req.require_offroad:
        parts.append("Offroad required")

    if parts and ctx.http_client_mod:
        guild = session.guild
        msg = f"<Bold>{guild.name}</> Passenger Requirements:\n" + "\n".join(parts)
        asyncio.create_task(
            show_popup(
                ctx.http_client_mod,
                msg,
                character_guid=str(character.guid),
                player_id=str(player.unique_id),
            )
        )

    return 0, 0, 0, 0


def _parse_timestamp(event):
    from amc.handlers.utils import parse_event_timestamp

    return parse_event_timestamp(event)
