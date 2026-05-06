"""Cargo event handlers.

Handles: ServerCargoArrived, ServerCargoDumped
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from operator import attrgetter

from django.contrib.gis.geos import Point
from django.db.models import F

from amc import config
from amc.handlers import register
from amc.models import (
    Delivery,
    DeliveryJob,
    DeliveryPoint,
    PoliceSession,
    ServerCargoArrivedLog,
    SubsidyRule,
    Wanted,
)
from amc.criminals import create_or_refresh_wanted
from amc.special_cargo import (
    ILLICIT_CARGO_KEYS,
    accumulate_illicit_delivery,
    link_delivery_to_criminal_record,
    should_trigger_wanted,
)
from amc.mod_detection import detect_custom_parts, POLICE_DUTY_WHITELIST
from amc.mod_server import (
    get_player_last_vehicle,
    get_player_last_vehicle_parts,
    show_popup,
)
from amc.fraud_detection import validate_cargo_payment
from amc.pipeline.discord import post_discord_delivery_embed
from amc.pipeline.delivery import atomic_process_delivery
from amc.police import SECURITY_BONUS_RATE, SECURITY_BONUS_MAX, get_active_police_count
from amc.subsidies import (
    apply_subsidy_player_cuts,
    get_subsidy_for_cargo,
    subsidise_player,
)
from amc.tax import (
    apply_tax_player_cuts,
    get_tax_for_cargo,
    record_tax_rule_collection,
    tax_player,
)
from amc_finance.services import record_ministry_subsidy_spend
from asgiref.sync import sync_to_async

logger = logging.getLogger("amc.webhook.handlers.cargo")


# ---------------------------------------------------------------------------
# ServerCargoDumped
# ---------------------------------------------------------------------------


@register("ServerCargoDumped")
async def handle_cargo_dumped(event, player, character, ctx):
    cargo = event["data"]["Cargo"]
    if cargo["Net_Payment"] < 0:
        raise ValueError(f"Negative payment for dumped cargo: {cargo}")

    cargo_data = cargo or {}
    log = await ServerCargoArrivedLog.objects.acreate(
        timestamp=_parse_timestamp(event),
        player=player,
        cargo_key=cargo_data.get("Net_CargoKey", ""),
        payment=cargo_data.get("Net_Payment", 0),
        weight=cargo_data.get("Net_Weight", 0),
        damage=cargo_data.get("Net_Damage", 0),
        data=event.get("data"),
    )
    subsidy, _, rule = await get_subsidy_for_cargo(log)
    if rule and subsidy > 0:
        await SubsidyRule.objects.filter(pk=rule.pk).aupdate(spent=F("spent") + subsidy)
    return log.payment, subsidy, 0, 0


# ---------------------------------------------------------------------------
# ServerCargoArrived
# ---------------------------------------------------------------------------


@register("ServerCargoArrived")
async def handle_cargo_arrived(event, player, character, ctx):
    from amc.cargo import get_cargo_bonus
    from amc.special_cargo import run_special_cargo_handlers
    from amc.supply_chain import check_and_record_contribution

    timestamp = _parse_timestamp(event)

    # --- 1. Parse cargos (all non-negative payments, including DeliveryId == 0) ---
    valid_cargos = _parse_cargos(event)

    # --- 2. Build logs (parallel) ---
    logs = await asyncio.gather(
        *[
            process_cargo_log(cargo, player, character, timestamp)
            for cargo in valid_cargos
        ]
    )

    # --- 3. Apply game-level bonuses (damage bonus etc.) ---
    for log in logs:
        log.payment += get_cargo_bonus(log.cargo_key, log.payment, log.damage or 0)

    # --- 4. Fraud detection ---
    for log in logs:
        excess = await validate_cargo_payment(
            cargo_key=log.cargo_key,
            payment=log.payment,
            quantity=1,
            sender_point=log.sender_point,
            destination_point=log.destination_point,
        )
        if excess > 0:
            log.payment = max(0, log.payment - excess)
            logger.warning(
                "Fraud detected (cargo): player=%s cargo=%s original=%d reduced=%d excess=%d",
                character.player.unique_id,
                log.cargo_key,
                log.payment + excess,
                log.payment,
                excess,
            )

    await ServerCargoArrivedLog.objects.abulk_create(logs)

    # --- 4b. Guild vehicle cargo requirement check ---
    from amc.guilds import check_guild_cargo

    guild_bonus_total = 0
    for log in logs:
        session, bonus = await check_guild_cargo(
            character, log.cargo_key, log.payment, log.damage or 0
        )
        if session:
            log.guild_session = session
            log.payment += bonus
            guild_bonus_total += bonus
    if guild_bonus_total > 0:
        await ServerCargoArrivedLog.objects.abulk_update(logs, ["guild_session", "payment"])

    for log in logs:
        if log.guild_session:
            from amc.guilds import check_guild_achievements
            await check_guild_achievements(character, log.guild_session, log, ctx.http_client_mod)

    # --- 5. Special cargo side effects are handled per-key inside the loop below

    # --- 6. Per-cargo-group: subsidy, delivery, job, supply chain ---
    total_subsidy = 0
    total_payment = sum(log.payment for log in logs)
    vehicle_key = character.last_vehicle_key or "" if character else ""

    key_by_cargo = attrgetter("cargo_key")
    logs.sort(key=key_by_cargo)

    for cargo_key, group in itertools.groupby(logs, key=key_by_cargo):
        group_list = list(group)
        quantity = len(group_list)
        payment = group_list[0].payment
        delivery_source = group_list[0].sender_point
        delivery_destination = group_list[0].destination_point

        is_illicit = cargo_key in ILLICIT_CARGO_KEYS

        cargo_name = group_list[0].get_cargo_key_display()

        # Wealth state is consulted by tax cuts, subsidy cuts, AND the
        # treasury-health subsidy clamp. Fetch it ONCE per delivery and
        # pass it through so we don't issue 3 duplicate aggregate queries
        # (lifetime income + bank balance) per cargo line.
        wealth_state = None
        if (
            config.WEALTH_TAX_SYSTEM_ENABLED
            and character
            and not character.is_gov_employee
            and not is_illicit
        ):
            from amc.subsidies import compute_wealth_state

            wealth_state = await compute_wealth_state(character)

        # --- ASEAN Tax (applied BEFORE subsidies, on raw base cargo payment) ---
        # Tax flows IN to the treasury. Computed on the pre-subsidy payment so
        # the player is taxed on the true base cargo value, not on the post-
        # subsidy boosted figure. Skipped for illicit cargo (criminal flow has
        # its own clawback) and for gov employees (their pay already redirects
        # to the treasury via wage-redirect; double-taxing is pointless).
        cargo_tax = 0
        if not is_illicit and not (character and character.is_gov_employee):
            tax_amount, _tax_factor, tax_rule = await get_tax_for_cargo(
                group_list[0],
                treasury_balance=ctx.treasury_balance,
                character=character,
            )
            cargo_tax = (tax_amount or 0) * quantity
            # Wealth-aware insulation: NEW players pay no tax (lifetime income < cap), established players pay between TAX_FLOOR_PCT
            cargo_tax = await apply_tax_player_cuts(
                cargo_tax,
                character,
                treasury_balance=ctx.treasury_balance,
                wealth_state=wealth_state,
            )
            if cargo_tax > 0:
                await tax_player(
                    cargo_tax,
                    character,
                    ctx.http_client_mod,
                    message=f"ASEAN Tax: {cargo_name}",
                )
                if tax_rule:
                    await record_tax_rule_collection(tax_rule, cargo_tax)

        cargo_subsidy = 0
        if not is_illicit and character and not character.is_gov_employee:
            cargo_subsidy_res = await get_subsidy_for_cargo(
                group_list[0], treasury_balance=ctx.treasury_balance
            )
            cargo_subsidy = cargo_subsidy_res[0] * quantity
            rule = cargo_subsidy_res[2]
            # Apply player-side cuts (treasury scale,rich players modded). Gov skip is enforced above so we
            # never reach apply_subsidy_player_cuts for a gov employee.
            cargo_subsidy = await apply_subsidy_player_cuts(
                cargo_subsidy,
                character,
                ctx.http_client_mod,
                treasury_balance=ctx.treasury_balance,
                wealth_state=wealth_state,
            )
            # Treasury self-heal clamp
            if cargo_subsidy > 0:
                from amc.subsidies import clamp_subsidy_for_treasury_health

                cargo_subsidy = await clamp_subsidy_for_treasury_health(
                    cargo_subsidy,
                    character,
                    ctx.treasury_balance,
                    wealth_state=wealth_state,
                )
            if rule and cargo_subsidy > 0:
                await SubsidyRule.objects.filter(pk=rule.pk).aupdate(
                    spent=F("spent") + cargo_subsidy
                )
                if ctx.active_term:
                    await record_ministry_subsidy_spend(cargo_subsidy, ctx.active_term.id)

        # Modded vehicle / on-foot detection for illicit cargo
        is_modded = False
        if is_illicit and ctx.http_client_mod:
            is_modded = await _check_modded_vehicle(
                character, ctx.http_client_mod
            )

        # Special cargo side effects (criminal level, criminal record, modded penalty)
        await run_special_cargo_handlers(
            group_list, character, ctx.http_client, ctx.http_client_mod,
            is_modded=is_modded,
        )

        # Find matching delivery job
        job = await (
            DeliveryJob.objects.filter_active().filter_by_delivery(
                delivery_source, delivery_destination, cargo_key
            )
        ).afirst()
        if job is not None and job.rp_mode and not ctx.is_rp_mode:
            job = None

        # Build delivery data
        delivery_data = _build_delivery_data(
            timestamp,
            character,
            cargo_key,
            quantity,
            payment,
            cargo_subsidy,
            delivery_source,
            delivery_destination,
            ctx.is_rp_mode,
        )

        # Per-player wealth/experience dim on the job's bonus_multiplier.
        if job is not None and not ctx.used_shortcut and character is not None:
            from amc.jobs import compute_payout_factor_for_character

            delivery_data["payout_factor"] = await compute_payout_factor_for_character(
                character,
                ctx.treasury_balance if ctx.treasury_balance is not None else 0.0,
                wealth_state=wealth_state,
            )

        job_id = job.id if job and not ctx.used_shortcut else None
        job = await sync_to_async(atomic_process_delivery)(
            job_id, quantity, delivery_data
        )

        # Job fulfillment
        if job and job.quantity_fulfilled >= job.quantity_requested:
            from amc.jobs import on_delivery_job_fulfilled

            rows_updated = await DeliveryJob.objects.filter(
                pk=job.id, fulfilled_at__isnull=True
            ).aupdate(fulfilled_at=timestamp)
            if rows_updated > 0:
                await job.arefresh_from_db()
                await on_delivery_job_fulfilled(job, ctx.http_client)

        # Supply chain contribution
        delivery_obj = await Delivery.objects.filter(
            character=character, cargo_key=cargo_key, timestamp=timestamp
        ).afirst()
        sc_bonus = 0
        if not is_illicit:
            sc_bonus = await check_and_record_contribution(
                delivery=delivery_obj,
                character=character,
                cargo_key=cargo_key,
                quantity=quantity,
                destination_point=delivery_destination,
                source_point=delivery_source,
            )
        delivery_subsidy = delivery_data["subsidy"] + sc_bonus

        # Wanted status for all illicit cargo (skipped for modded criminals)
        if is_illicit and character and not is_modded:
            delivery_amount = payment * quantity
            # Accumulate within the debounce window so splitting across multiple
            # small deliveries (~5 s apart) is treated the same as one big one.
            accumulated_amount = await accumulate_illicit_delivery(
                character.guid, delivery_amount
            )
            # Check if already wanted (always refresh) or roll probability
            already_wanted = await Wanted.objects.filter(
                character=character, expired_at__isnull=True
            ).aexists()
            if already_wanted or should_trigger_wanted(accumulated_amount):
                # Bounty (Wanted.amount) starts at 0 — it only grows from police
                # proximity during chase, tracked in tick_wanted_countdown.
                # Delivery payments are confiscated via CriminalRecord.confiscatable_amount.
                wanted, newly_created = await create_or_refresh_wanted(
                    character,
                    ctx.http_client_mod,
                    amount=0,
                    wanted_remaining=Wanted.INITIAL_WANTED_LEVEL,
                )
                # Announce only when a new Wanted record is created
                if newly_created and ctx.http_client:
                    from amc.special_cargo import _announce_laundered_after_delay
                    from django.core.cache import cache

                    cache_key = f"money_laundered:{character.guid}"
                    await cache.aset(
                        cache_key,
                        {"total": delivery_amount, "name": character.name},
                        timeout=60,
                    )
                    asyncio.create_task(
                        _announce_laundered_after_delay(
                            character.guid, ctx.http_client, delay=15
                        )
                    )
            # Link delivery to the criminal record — always, not just when wanted triggers
            if delivery_obj:
                await link_delivery_to_criminal_record(
                    character, cargo_key, timestamp
                )

        # Discord notification — suppressed for illicit cargo to avoid revealing
        # criminal activity in a public channel.
        if ctx.discord_client and not is_illicit:
            asyncio.create_task(
                post_discord_delivery_embed(
                    ctx.discord_client,
                    character,
                    cargo_name,
                    quantity,
                    delivery_source,
                    delivery_destination,
                    payment * quantity,
                    delivery_subsidy,
                    vehicle_key,
                    job=job,
                    delivery_id=delivery_obj.id if delivery_obj else None,
                )
            )

        total_subsidy += delivery_subsidy
        # Net out tax from the reported base payment so downstream savings,
        # loan repayment and profit calculations operate on the player''s
        # actual take-home from this cargo group.
        if cargo_tax > 0:
            total_payment -= cargo_tax

        # Risk premium for Money deliveries
        if cargo_key == "Money" and ctx.http_client_mod:
            active_police = await get_active_police_count()
            if active_police > 0:
                bonus_pct = min(active_police * SECURITY_BONUS_RATE, SECURITY_BONUS_MAX)
                risk_premium = int(payment * quantity * bonus_pct)
                if risk_premium > 0:
                    await subsidise_player(
                        risk_premium, character, ctx.http_client_mod,
                        message="Risk Premium",
                    )

    return total_payment, total_subsidy, 0, 0


# ---------------------------------------------------------------------------
# Sub-helpers for handle_cargo_arrived
# ---------------------------------------------------------------------------


def _parse_cargos(event):
    """Extract cargos from a ServerCargoArrived event.

    All cargos with non-negative payments are returned for processing.
    DeliveryId is informational only — a value of 0 or absence of the key
    no longer causes a cargo to be dropped. Fraud detection and illicit cargo
    handling still apply to every item.
    """
    valid_cargos = []
    for cargo in event["data"]["Cargos"]:
        if cargo["Net_Payment"] < 0:
            raise ValueError(f"Negative payment for cargo: {cargo}")
        if cargo.get("Net_DeliveryId", -1) == 0:
            logger.debug(
                "Processing cargo with DeliveryId=0 (non-job delivery): %s",
                cargo.get("Net_CargoKey"),
            )
        valid_cargos.append(cargo)
    return valid_cargos


async def _check_modded_vehicle(
    character, http_client_mod
) -> bool:
    """Check for modded parts or on-foot delivery of illicit cargo.

    Returns True if modded parts were detected or player is on foot.
    Wallet deduction is handled by the special cargo handlers.
    """
    try:
        last_vehicle, parts_data = await asyncio.gather(
            get_player_last_vehicle(http_client_mod, str(character.guid)),
            get_player_last_vehicle_parts(
                http_client_mod, str(character.guid), complete=False
            ),
        )
        main_vehicle = last_vehicle.get("vehicle")
        if not main_vehicle:
            asyncio.create_task(
                show_popup(
                    http_client_mod,
                    "Your criminal profits were zeroed out for delivering on foot.",
                    character_guid=character.guid,
                    player_id=str(character.player.unique_id),
                )
            )
            return True

        whitelist = None
        is_on_duty = await PoliceSession.objects.filter(
            character=character, ended_at__isnull=True
        ).aexists()
        if is_on_duty:
            whitelist = POLICE_DUTY_WHITELIST
        custom_parts = detect_custom_parts(
            parts_data.get("parts", []), whitelist=whitelist
        )
        if custom_parts:
            asyncio.create_task(
                show_popup(
                    http_client_mod,
                    "Your criminal profits were zeroed out for using a modified vehicle.",
                    character_guid=character.guid,
                    player_id=str(character.player.unique_id),
                )
            )
            return True
        return False
    except Exception as e:
        logger.warning(f"Failed to check custom parts for money delivery penalty: {e}")
        return False


def _build_delivery_data(
    timestamp,
    character,
    cargo_key,
    quantity,
    payment,
    subsidy,
    delivery_source,
    delivery_destination,
    is_rp_mode,
):
    """Construct the delivery_data dict used for Delivery creation."""
    delivery_data = {
        "timestamp": timestamp,
        "character": character,
        "cargo_key": cargo_key,
        "quantity": quantity,
        "payment": payment * quantity,
        "subsidy": subsidy,
        "sender_point": delivery_source,
        "destination_point": delivery_destination,
        "rp_mode": is_rp_mode,
    }
    if is_rp_mode:
        delivery_data["subsidy"] = int((subsidy * 1.5) + (payment * quantity * 0.5))
    return delivery_data


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def process_cargo_log(cargo, player, character, timestamp):
    """Create a ServerCargoArrivedLog from raw cargo event data."""
    sender_coord_raw = cargo["Net_SenderAbsoluteLocation"]
    sender_coord = Point(
        sender_coord_raw["X"],
        sender_coord_raw["Y"],
        sender_coord_raw["Z"],
    ).buffer(1)
    destination_coord_raw = cargo["Net_DestinationLocation"]
    destination_coord = Point(
        destination_coord_raw["X"],
        destination_coord_raw["Y"],
        destination_coord_raw["Z"],
    ).buffer(1)
    sender = await DeliveryPoint.objects.filter(coord__coveredby=sender_coord).afirst()
    destination = await DeliveryPoint.objects.filter(
        coord__coveredby=destination_coord
    ).afirst()
    return ServerCargoArrivedLog(
        timestamp=timestamp,
        player=player,
        character=character,
        cargo_key=cargo["Net_CargoKey"],
        payment=cargo["Net_Payment"],
        weight=cargo.get("Net_Weight", 0),
        damage=cargo["Net_Damage"],
        sender_point=sender,
        destination_point=destination,
        data=cargo,
    )


def _parse_timestamp(event):
    from amc.handlers.utils import parse_event_timestamp

    return parse_event_timestamp(event)
