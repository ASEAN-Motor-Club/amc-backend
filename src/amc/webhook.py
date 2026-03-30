import json
import logging
import os
import asyncio
import itertools
from operator import attrgetter
from datetime import timedelta
from django.utils import timezone
from django.contrib.gis.geos import Point
from django.db import transaction
from asgiref.sync import sync_to_async
from django.db.models import F, Q
from typing import Any, cast
from amc.game_server import announce
from amc.utils import skip_if_running
from amc.mod_server import despawn_player_cargo, get_webhook_events2, send_system_message, show_popup, get_rp_mode, transfer_money, get_patrol_point_payments, get_parties, get_party_members_for_character, list_player_vehicles
from amc.mod_detection import detect_custom_parts, POLICE_DUTY_WHITELIST
from amc.subsidies import (
    repay_loan_for_profit,
    set_aside_player_savings,
    get_subsidy_for_cargo,
    get_passenger_subsidy,
    subsidise_player,
)
from amc_finance.services import (
    get_treasury_fund_balance,
    record_ministry_subsidy_spend,
    record_treasury_confiscation_income,
    send_fund_to_player_wallet,
)
from django.core.cache import cache
from amc.jobs import on_delivery_job_fulfilled
from amc.models import (
    Player,
    ServerCargoArrivedLog,
    ServerSignContractLog,
    ServerPassengerArrivedLog,
    ServerTowRequestArrivedLog,
    PolicePatrolLog,
    PolicePenaltyLog,
    PoliceShiftLog,
    Delivery,
    DeliveryPoint,
    DeliveryJob,
    Character,
    Confiscation,
    MinistryTerm,
    PoliceSession,
    SubsidyRule,
)

logger = logging.getLogger("amc.webhook")

PARTY_BONUS_ENABLED = os.environ.get("PARTY_BONUS_ENABLED", "").lower() in ("1", "true", "yes")
WEBHOOK_SSE_ENABLED = os.environ.get("WEBHOOK_SSE_ENABLED", "").lower() in ("1", "true", "yes")
PARTY_BONUS_RATE = 0.05  # 5% per extra party member


async def on_player_profits(player_profits, session, http_client=None):
    for character, subsidy, base_payment, contract_payment in player_profits:
        await on_player_profit(
            character, subsidy, base_payment, session, http_client,
            contract_payment=contract_payment,
        )


async def on_player_profit(
    character, subsidy, base_payment, session, http_client=None,
    contract_payment=0,
):
    """Process a player's profit after party splitting.

    Args:
        character: The Character receiving the payment.
        subsidy: Subsidy portion (paid separately from wallet, not baked in).
        base_payment: What the game actually deposited into the wallet
            (excludes subsidy and contract).
        contract_payment: Contract completion payment deposited into wallet.
        session: HTTP client for mod server calls.
        http_client: HTTP client for API calls.
    """
    if character.reject_ubi:
        subsidy = 0

    if character.is_gov_employee:
        from amc.gov_employee import redirect_income_to_treasury

        # Gov employees: confiscate wallet deposits, redirect to treasury.
        # Contract payment is burned — confiscated but NOT sent to treasury.
        wallet_confiscation = base_payment + contract_payment
        if wallet_confiscation > 0:
            await transfer_money(
                session,
                int(-wallet_confiscation),
                "Government Service",
                str(character.player.unique_id),
            )
            if base_payment > 0:
                # Ledger: real earnings confiscated (excludes burned contracts)
                await redirect_income_to_treasury(
                    base_payment,
                    character,
                    "Government Service – Earnings",
                    http_client=http_client,
                    session=session,
                )

        # Subsidy contribution (e.g. depot restock):
        # Send subsidy to wallet, then confiscate it back for visible transactions.
        # amount=0: no treasury donation (the subsidy came FROM the treasury).
        # contribution=subsidy: counts toward gov level progression.
        if subsidy > 0:
            await subsidise_player(subsidy, character, session)
            await transfer_money(
                session,
                int(-subsidy),
                "Government Service",
                str(character.player.unique_id),
            )
            await redirect_income_to_treasury(
                0,
                character,
                "Government Service – Subsidy",
                http_client=http_client,
                session=session,
                contribution=subsidy,
            )
        return

    if subsidy != 0:
        await subsidise_player(subsidy, character, session)
    actual_income = base_payment + subsidy + contract_payment
    loan_repayment = await repay_loan_for_profit(character, actual_income, session)
    savings = actual_income - loan_repayment
    if savings > 0:
        await set_aside_player_savings(character, savings, session)


async def post_discord_delivery_embed(
    discord_client,
    character,
    cargo_key,
    quantity,
    delivery_source,
    delivery_destination,
    payment,
    subsidy,
    vehicle_key,
    job=None,
    delivery_id=None,
):
    jobs_cog = discord_client.get_cog("JobsCog")
    delivery_source_name = ""
    delivery_destination_name = ""
    if delivery_source:
        delivery_source_name = delivery_source.name
    if delivery_destination:
        delivery_destination_name = delivery_destination.name

    if jobs_cog and hasattr(jobs_cog, "post_delivery_embed"):
        loop = asyncio.get_running_loop()
        loop.run_in_executor(
            None,
            lambda: asyncio.run_coroutine_threadsafe(
                jobs_cog.post_delivery_embed(
                    character.name,
                    cargo_key,
                    quantity,
                    delivery_source_name,
                    delivery_destination_name,
                    payment,
                    subsidy,
                    vehicle_key,
                    job=job,
                    delivery_id=delivery_id,
                ),
                discord_client.loop,
            ),
        )


@skip_if_running
async def monitor_webhook(ctx):
    http_client = ctx.get("http_client")
    http_client_mod = ctx.get("http_client_mod")
    http_client_webhook = ctx.get("http_client_webhook")
    discord_client = ctx.get("discord_client")
    events = await get_webhook_events2(http_client_webhook)
    await process_events(events, http_client, http_client_mod, discord_client)


@skip_if_running
async def monitor_webhook_test(ctx):
    http_client = ctx.get("http_client_test")
    http_client_mod = ctx.get("http_client_test_mod")
    http_client_webhook = ctx.get("http_client_test_webhook")
    discord_client = ctx.get("discord_client")
    try:
        events = await get_webhook_events2(http_client_webhook)
    except Exception as e:
        print(f"Failed to get webhook events: {e}")
        return
    await process_events(events, http_client, http_client_mod, discord_client)


async def handle_cargo_dumped(event, player, timestamp):
    cargo = event["data"]["Cargo"]
    if cargo["Net_Payment"] < 0:
        raise ValueError(f"Negative payment for dumped cargo: {cargo}")

    cargo_data = cargo or {}
    log = await ServerCargoArrivedLog.objects.acreate(
        timestamp=timestamp,
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
    return log.payment, subsidy


async def handle_contract_signed(event, player, timestamp):
    contract = event["data"].get("Contract")
    if not contract:
        raise ValueError(f"Missing contract data in event: {event}")

    await ServerSignContractLog.objects.acreate(
        timestamp=timestamp,
        player=player,
        guid=event["data"].get("ContractGuid"),  # None for old mod, set for new mod
        cargo_key=contract.get("Item", ""),
        amount=contract.get("Amount", 0),
        payment=contract.get("CompletionPayment", {}).get("BaseValue", 0),
        cost=contract.get("Cost", {}).get("BaseValue", 0),
    )


async def handle_contract_delivered(event, player, timestamp):
    data = event.get("data", {})
    guid = data.get("ContractGuid")
    if not guid:
        raise ValueError("Missing ContractGuid")

    if "Item" in data:
        # Old mod: contract data included in each delivery event.
        # Create log if it doesn't exist (fallback for missing ServerSignContract).
        log, _ = await ServerSignContractLog.objects.aget_or_create(
            guid=guid,
            defaults={
                "timestamp": timestamp,
                "player": player,
                "cargo_key": data["Item"],
                "amount": data["Amount"],
                "payment": data["CompletionPayment"],
                "cost": data.get("Cost", 0),
                "data": data,
            },
        )
    else:
        # New mod: guid-only, log already exists from ServerSignContract hook.
        try:
            log = await ServerSignContractLog.objects.aget(guid=guid)
        except ServerSignContractLog.DoesNotExist:
            return 0, 0

    log.finished_amount = cast(Any, F("finished_amount") + 1)
    await log.asave(update_fields=["finished_amount"])
    await log.arefresh_from_db()

    payment = 0
    if log.finished_amount == log.amount and not log.delivered:
        payment = log.payment
        log.delivered = True
        await log.asave(update_fields=["delivered"])

    return payment, 0


async def handle_passenger_arrived(event, player, timestamp, character=None, http_client_mod=None):
    passenger = event["data"].get("Passenger")
    passenger_data = passenger or {}
    base_payment = passenger_data.get("Net_Payment", 0)
    flag = passenger_data.get("Net_PassengerFlags", 0)

    if base_payment < 0:
        raise ValueError(f"Negative payment for passenger: {passenger_data}")

    # Exploit detection: passengers picked up on a modded server have
    # Net_StartLocation at the world origin (0,0,0).  Log for audit but
    # claw back the game-deposited payment and suppress all payouts.
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
        # Claw back the payment the game already deposited
        if base_payment > 0 and character and http_client_mod:
            await transfer_money(
                http_client_mod,
                int(-base_payment),
                "Invalid Passenger",
                str(character.player.unique_id),
            )
            asyncio.create_task(
                show_popup(
                    http_client_mod,
                    "Passenger delivery rejected: invalid origin.",
                    character_guid=character.guid,
                    player_id=str(character.player.unique_id),
                )
            )
        logger.warning(
            "Exploit detected: passenger with zero start location for player %s (payment=%s)",
            player.unique_id, base_payment,
        )
        return 0, 0

    if log.passenger_type == ServerPassengerArrivedLog.PassengerType.Taxi:
        if log.comfort:
            bonus_per_star = 0.2
            if log.limo:
                bonus_per_star = bonus_per_star * 1.3
            log.payment += base_payment * log.comfort_rating * bonus_per_star
        if log.urgent:
            log.payment += base_payment * log.urgent_rating * 0.3

    if log.passenger_type == ServerPassengerArrivedLog.PassengerType.Ambulance:
        radius_ratio = passenger_data.get("Net_SearchAndRescueRadiusRatio")
        if radius_ratio is not None:
            bonus_multiplier = 1 - radius_ratio  # smaller radius = higher bonus
            log.payment += int(base_payment * bonus_multiplier)

    await log.asave()
    subsidy = get_passenger_subsidy(log)
    return log.payment, subsidy


async def handle_tow_request(event, player, timestamp):
    tow_request = event["data"].get("TowRequest")
    tow_data = tow_request or {}
    payment = tow_data.get("Net_Payment", 0)

    # Body damage bonus: full bonus at 0 damage, scales to 0 at full damage.
    # The game deposits this on top of Net_Payment into the player's wallet.
    # Max bonus rate ~55% of base payment (derived from game data).
    body_damage = tow_data.get("BodyDamage", 1.0)  # default 1.0 = no bonus
    payment += int(payment * 0.55 * (1 - body_damage))

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

    return payment, subsidy


async def handle_patrol_arrived(event, player, timestamp, http_client_mod=None):
    patrol_point_id = event["data"].get("PatrolPointId", 0)
    base_payment = 0
    area_bonus_payment = 0

    if http_client_mod:
        payments = await get_patrol_point_payments(http_client_mod)
        if patrol_point_id in payments:
            base_payment = payments[patrol_point_id]["BasePayment"]
            area_bonus_payment = payments[patrol_point_id]["AreaBonusPayment"]

    await PolicePatrolLog.objects.acreate(
        timestamp=timestamp,
        player=player,
        patrol_point_id=patrol_point_id,
        base_payment=base_payment,
        area_bonus_payment=area_bonus_payment,
        data=event.get("data"),
    )
    return 0, 0


async def handle_police_penalty(event, player, timestamp):
    warning_only = event["data"].get("bWarningOnly", False)
    await PolicePenaltyLog.objects.acreate(
        timestamp=timestamp,
        player=player,
        warning_only=warning_only,
        data=event.get("data"),
    )
    return 0, 0


async def handle_police_shift(event, player, timestamp, action):
    await PoliceShiftLog.objects.acreate(
        timestamp=timestamp,
        player=player,
        action=action,
        data=event.get("data"),
    )
    return 0, 0


SMUGGLING_TIPOFF_ENABLED = os.environ.get("SMUGGLING_TIPOFF_ENABLED", "").lower() in ("1", "true", "yes")
SMUGGLING_TIPOFF_DELAY = 15  # seconds — delay before broadcasting
SMUGGLING_TIPOFF_COOLDOWN = 60  # seconds — throttle window per player


async def _announce_smuggling_tipoff_after_delay(http_client, delay=SMUGGLING_TIPOFF_DELAY):
    """Wait for the delay, then announce a vague smuggling tip-off."""
    await asyncio.sleep(delay)
    await announce(
        "Intelligence reports suggest a smuggling operation is underway",
        http_client,
        color="E67E22",
    )


async def _announce_confiscation_after_delay(character_guid, http_client, delay=30):
    """Wait for the debounce window, then announce the accumulated confiscation total."""
    await asyncio.sleep(delay)
    cache_key = f"money_confiscated:{character_guid}"
    total = await cache.aget(cache_key, 0)
    await cache.adelete(cache_key)
    if total > 0:
        await announce(
            f"${total:,} has been confiscated by police",
            http_client,
            color="4A90D9",
        )


async def handle_pickup_cargo(event, character, http_client, http_client_mod):
    """Handle ServerPickupCargo: confiscate Money if picker is police."""
    cargo = event["data"].get("Cargo", {})
    cargo_key = cargo.get("Net_CargoKey")
    if cargo_key != "Money":
        return

    # Must be active police (on duty)
    is_police = await PoliceSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aexists()
    if not is_police:
        return

    # Police picking up money always despawns the cargo, regardless of
    # whether the confiscation itself is valid (self-pickup, police-on-police, etc.)
    try:
        await despawn_player_cargo(http_client_mod, str(character.guid))
    except Exception:
        logger.warning("Failed to despawn money cargo for police %s", character.guid)

    payment = cargo.get("Net_Payment", 0)
    previous_owner_guid = cargo.get("PreviousOwnerCharacterGuid")
    if not previous_owner_guid or payment <= 0:
        return

    # No self-confiscation
    if str(character.guid).upper() == previous_owner_guid.upper():
        return

    # Look up previous owner
    previous_owner = await (
        Character.objects.select_related("player")
        .filter(guid=previous_owner_guid)
        .afirst()
    )

    is_prev_police = False
    if previous_owner:
        # No police-on-police confiscation
        is_prev_police = await PoliceSession.objects.filter(
            character=previous_owner, ended_at__isnull=True
        ).aexists()
    if is_prev_police:
        return

    # 1. Record confiscation
    await Confiscation.objects.acreate(
        character=previous_owner,
        officer=character,
        cargo_key=cargo_key,
        amount=payment,
    )

    # 2. Charge previous owner
    if previous_owner:
        await transfer_money(
            http_client_mod,
            int(-payment),
            "Money Confiscated",
            str(previous_owner.player.unique_id),
        )

    # 3. Credit treasury
    await record_treasury_confiscation_income(payment, "Police Confiscation")

    # 4. Debounced announcement
    if http_client:
        cache_key = f"money_confiscated:{character.guid}"
        prev_total = await cache.aget(cache_key, 0)
        if prev_total == 0:
            await cache.aset(cache_key, payment, timeout=60)
            asyncio.create_task(
                _announce_confiscation_after_delay(
                    character.guid, http_client, delay=30
                )
            )
        else:
            await cache.aset(cache_key, prev_total + payment, timeout=60)

    # 5. Track confiscation for police level
    from amc.police import record_confiscation_for_level

    await record_confiscation_for_level(
        character, payment, http_client=http_client, session=http_client_mod
    )

    # 6. Reward officer with confiscated amount
    if http_client_mod:
        await transfer_money(
            http_client_mod,
            int(payment),
            "Confiscation Reward",
            str(character.player.unique_id),
        )
        await send_fund_to_player_wallet(payment, character, "Confiscation Reward")
        await send_system_message(
            http_client_mod,
            f"You earned ${payment:,} confiscation reward.",
            character_guid=character.guid,
        )


async def handle_reset_vehicle(character, timestamp, is_rp_mode, http_client):
    if is_rp_mode and character.last_login < timestamp - timedelta(seconds=15):
        # await despawn_player_vehicle(http_client_mod, player.unique_id)
        asyncio.create_task(
            announce(
                f"{character.name}'s vehicle has been despawned for using roadside recovery while on RP mode",
                http_client,
                color="FFA500",
            )
        )


TELEPORT_PENALTY_WINDOW = 10  # minutes — same as ARREST_CONFISCATION_WINDOW
TELEPORT_PENALTY_ANNOUNCE_DELAY = 10  # seconds — debounce window for announcements


async def _announce_teleport_penalty_after_delay(
    character_guid, character_name, player_unique_id, http_client, http_client_mod, delay=TELEPORT_PENALTY_ANNOUNCE_DELAY,
):
    """Wait for the debounce window, then announce the accumulated teleport penalty."""
    await asyncio.sleep(delay)
    cache_key = f"teleport_penalty:{character_guid}"
    total = await cache.aget(cache_key, 0)
    await cache.adelete(cache_key)
    if total <= 0:
        return
    if http_client_mod:
        asyncio.create_task(
            show_popup(
                http_client_mod,
                f"You lost ${total:,} for teleporting during criminal cooldown.",
                character_guid=character_guid,
                player_id=player_unique_id,
            )
        )
    if http_client:
        await announce(
            f"{character_name} lost ${total:,} for teleporting during criminal cooldown",
            http_client,
            color="E74C3C",
        )


async def handle_teleport_or_respawn(event, character, timestamp, http_client_mod, http_client):
    """Penalise criminals who teleport/reset within the confiscation window.

    Uses the same linear decay formula as police arrest confiscation:
    rate = max(0, 1 - elapsed_minutes / window). The penalty is deducted
    from the player's wallet and criminal_laundered_total is reversed.
    """
    # Skip police officers — they don't deliver Money
    is_police = await PoliceSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aexists()
    if is_police:
        return

    # Find recent Money deliveries within the penalty window
    window_start = timestamp - timedelta(minutes=TELEPORT_PENALTY_WINDOW)
    recent_deliveries = [
        d async for d in Delivery.objects.filter(
            character=character,
            cargo_key="Money",
            timestamp__gte=window_start,
        )
    ]
    if not recent_deliveries:
        return

    # Calculate penalty using linear decay per delivery
    penalty = 0
    for delivery in recent_deliveries:
        elapsed_minutes = (timestamp - delivery.timestamp).total_seconds() / 60
        rate = max(0.0, 1.0 - elapsed_minutes / TELEPORT_PENALTY_WINDOW)
        penalty += round(delivery.payment * rate)

    if penalty <= 0:
        return

    # 1. Deduct from wallet
    if http_client_mod:
        await transfer_money(
            http_client_mod,
            int(-penalty),
            "Teleport Penalty",
            str(character.player.unique_id),
        )

    # 2. Reverse criminal_laundered_total (clamp to 0)
    await character.arefresh_from_db(fields=["criminal_laundered_total"])
    new_total = max(0, character.criminal_laundered_total - penalty)
    character.criminal_laundered_total = new_total
    await character.asave(update_fields=["criminal_laundered_total"])

    # 3. Record as Confiscation with officer=None (self-inflicted)
    #    This suppresses the "money is now safe" announcement in special_cargo.py
    await Confiscation.objects.acreate(
        character=character,
        officer=None,
        cargo_key="Money",
        amount=penalty,
    )

    # 4. Refresh player name tag (criminal level may have dropped)
    from amc.player_tags import refresh_player_name
    await refresh_player_name(character, http_client_mod)

    # 5. Debounced popup + announcement
    cache_key = f"teleport_penalty:{character.guid}"
    prev_total = await cache.aget(cache_key, 0)
    new_total = prev_total + penalty
    await cache.aset(cache_key, new_total, timeout=30)
    if prev_total == 0:
        asyncio.create_task(
            _announce_teleport_penalty_after_delay(
                character.guid,
                character.name,
                str(character.player.unique_id),
                http_client,
                http_client_mod,
            )
        )




async def handle_cargo_arrived(
    event,
    player,
    character,
    timestamp,
    treasury_balance,
    is_rp_mode,
    used_shortcut,
    http_client,
    http_client_mod=None,
    discord_client=None,
    active_term=None,
):
    valid_cargos = []
    for cargo in event["data"]["Cargos"]:
        if cargo["Net_Payment"] < 0:
            raise ValueError(f"Negative payment for cargo: {cargo}")
        valid_cargos.append(cargo)

    logs = await asyncio.gather(
        *[
            process_cargo_log(cargo, player, character, timestamp)
            for cargo in valid_cargos
        ]
    )

    from amc.cargo import get_cargo_bonus

    # Add game-level bonuses (damage bonus etc.) to each log's payment
    # so it persists in ServerCargoArrivedLog and flows through to Delivery
    for log in logs:
        log.payment += get_cargo_bonus(log.cargo_key, log.payment, log.damage or 0)

    await ServerCargoArrivedLog.objects.abulk_create(logs)

    # --- Special cargo side effects (e.g. Money → criminal record, announcements) ---
    from amc.special_cargo import run_special_cargo_handlers

    await run_special_cargo_handlers(logs, character, http_client, http_client_mod)

    total_subsidy = 0
    total_payment = sum([log.payment for log in logs])

    vehicle_key = ""
    if character:
        vehicle_key = character.last_vehicle_key or ""

    key_by_cargo = attrgetter("cargo_key")
    logs.sort(key=key_by_cargo)
    for cargo_key, group in itertools.groupby(logs, key=key_by_cargo):
        group_list = list(group)
        quantity = len(group_list)
        payment = group_list[0].payment
        delivery_source = group_list[0].sender_point
        delivery_destination = group_list[0].destination_point
        cargo_subsidy_res = await get_subsidy_for_cargo(
            group_list[0], treasury_balance=treasury_balance
        )
        cargo_subsidy = cargo_subsidy_res[0] * quantity
        rule = cargo_subsidy_res[2]
        if rule and cargo_subsidy > 0:
            await SubsidyRule.objects.filter(pk=rule.pk).aupdate(
                spent=F("spent") + cargo_subsidy
            )
            if active_term:
                await record_ministry_subsidy_spend(cargo_subsidy, active_term.id)
        cargo_name = group_list[0].get_cargo_key_display()

        if cargo_key == "Money" and http_client_mod:
            try:
                vehicles = await list_player_vehicles(http_client_mod, str(character.player.unique_id), active=True, complete=True)
                if vehicles:
                    main_vehicle = next((v for v in vehicles.values() if v.get("isLastVehicle") and v.get("index", -1) == 0), None)
                    if main_vehicle:
                        # Whitelist police parts for officers on active duty
                        whitelist = None
                        is_on_duty = await PoliceSession.objects.filter(
                            character=character, ended_at__isnull=True
                        ).aexists()
                        if is_on_duty:
                            whitelist = POLICE_DUTY_WHITELIST
                        custom_parts = detect_custom_parts(main_vehicle.get("parts", []), whitelist=whitelist)
                        if custom_parts:
                            penalty = payment * quantity
                            await transfer_money(
                                http_client_mod,
                                int(-penalty),
                                "Modded Vehicle Penalty",
                                str(character.player.unique_id),
                            )
                            asyncio.create_task(
                                show_popup(
                                    http_client_mod,
                                    "Your criminal profits were zeroed out for using a modified vehicle.",
                                    character_guid=character.guid,
                                    player_id=str(character.player.unique_id),
                                )
                            )
            except Exception as e:
                logger.warning(f"Failed to check custom parts for money delivery penalty: {e}")

        job = await (
            DeliveryJob.objects.filter_active().filter_by_delivery(
                delivery_source, delivery_destination, cargo_key
            )
        ).afirst()
        if job is not None and job.rp_mode and not is_rp_mode:
            job = None

        delivery_data = {
            "timestamp": timestamp,
            "character": character,
            "cargo_key": cargo_key,
            "quantity": quantity,
            "payment": payment * quantity,
            "subsidy": cargo_subsidy,
            "sender_point": delivery_source,
            "destination_point": delivery_destination,
            "rp_mode": is_rp_mode,
        }

        if is_rp_mode:
            # fixed bug: using cargo_subsidy instead of accumulator
            delivery_data["subsidy"] = int(
                (cargo_subsidy * 1.5) + (payment * quantity * 0.5)
            )

        job_id = job.id if job and not used_shortcut else None

        job = await sync_to_async(atomic_process_delivery)(
            job_id, quantity, delivery_data
        )

        if job and job.quantity_fulfilled >= job.quantity_requested:
            rows_updated = await DeliveryJob.objects.filter(
                pk=job.id, fulfilled_at__isnull=True
            ).aupdate(fulfilled_at=timestamp)

            if rows_updated > 0:
                await job.arefresh_from_db()
                await on_delivery_job_fulfilled(job, http_client)

        # Supply chain event contribution tracking
        from amc.supply_chain import check_and_record_contribution

        delivery_obj = await Delivery.objects.filter(
            character=character, cargo_key=cargo_key, timestamp=timestamp
        ).afirst()
        sc_bonus = await check_and_record_contribution(
            delivery=delivery_obj,
            character=character,
            cargo_key=cargo_key,
            quantity=quantity,
            destination_point=delivery_destination,
            source_point=delivery_source,
        )
        delivery_subsidy = delivery_data["subsidy"] + sc_bonus

        # Risk premium: Money deliveries get extra payout based on active police count
        security_bonus = 0
        if cargo_key == "Money":
            from amc.police import get_active_police_count, SECURITY_BONUS_RATE, SECURITY_BONUS_MAX
            police_count = await get_active_police_count()
            bonus_rate = min(police_count * SECURITY_BONUS_RATE, SECURITY_BONUS_MAX)
            security_bonus = int(payment * quantity * bonus_rate)
            if security_bonus > 0 and character:
                await subsidise_player(security_bonus, character, http_client_mod, message="Risk Premium")

        if discord_client:
            asyncio.create_task(
                post_discord_delivery_embed(
                    discord_client,
                    character,
                    cargo_name,
                    quantity,
                    delivery_source,
                    delivery_destination,
                    payment * quantity,
                    delivery_subsidy + security_bonus,
                    vehicle_key,
                    job=job,
                    delivery_id=delivery_obj.id if delivery_obj else None,
                )
            )

        total_subsidy += delivery_subsidy

    return total_payment, total_subsidy


def aggregate_homogenous_events(sorted_events):
    grouped_events = itertools.groupby(
        sorted_events, key=lambda e: (e["key_id"], e["hook"])
    )
    aggregated_events = []

    for key, group in grouped_events:
        if not key[0]:  # key_id
            continue

        group_events = list(group)
        match key[1]:  # hook
            case "ServerCargoArrived":
                cargos = [
                    cargo for event in group_events for cargo in event["data"]["Cargos"]
                ]
                aggregated_events.append(
                    {
                        "hook": key[1],
                        "timestamp": group_events[0]["timestamp"],
                        "data": {
                            "CharacterGuid": key[0],
                            "Cargos": cargos,
                        },
                    }
                )
            case "ServerResetVehicleAtResponse":
                aggregated_events.append(
                    {
                        "hook": key[1],
                        "timestamp": group_events[0]["timestamp"],
                        "data": {
                            "CharacterGuid": key[0],
                            "VehicleId": group_events[0]["data"].get("VehicleId"),
                        },
                    }
                )
            case _:
                aggregated_events.extend(group_events)
    return aggregated_events


async def split_party_payment(
    character, parties,
    total_base_payment, total_subsidy, total_contract_payment,
    http_client_mod, used_shortcut=False,
):
    """Split payment among party members, applying party bonus.

    Returns a list of (character, subsidy, base_payment, contract_payment) tuples
    for all party members, or None if the character is not in a multi-person party.

    The party bonus is calculated as a percentage of base_payment and added
    as extra subsidy. Wallet transfers move base_share + contract_share from
    the earner to each other member. Any integer division remainder stays with
    the earner.
    """
    if not PARTY_BONUS_ENABLED:
        return None
    if total_base_payment <= 0 and total_contract_payment <= 0:
        return None

    member_guids = get_party_members_for_character(parties, str(character.guid))
    party_size = len(member_guids)
    if party_size <= 1:
        return None

    # 1. Party bonus: percentage of base payment, added as subsidy only.
    # The game didn't deposit this, the system pays it via on_player_profit.
    party_multiplier = 1 + (party_size - 1) * PARTY_BONUS_RATE
    party_bonus = int(total_base_payment * (party_multiplier - 1))
    total_subsidy += party_bonus

    # 2. Equal split (remainder stays with earner via on_player_profit)
    share_base = total_base_payment // party_size
    share_subsidy = total_subsidy // party_size
    share_contract = total_contract_payment // party_size

    # 3. Look up other party members
    other_guids = [g for g in member_guids if g.upper() != str(character.guid).upper()]
    other_characters = []
    if other_guids:
        other_characters = [
            c async for c in Character.objects.filter(
                guid__in=other_guids
            ).select_related("player")
        ]

    # 4. Wallet transfers
    # The game deposited base earnings + contract payment into earner's wallet.
    # Subsidy is paid separately by on_player_profit.
    # Transfer each other member's base + contract share.
    wallet_share = share_base + share_contract
    others_withdrawal = wallet_share * len(other_characters)
    if others_withdrawal > 0 and http_client_mod:
        await transfer_money(
            http_client_mod,
            int(-others_withdrawal),
            "Party Split",
            str(character.player.unique_id),
        )

    for other_char in other_characters:
        if wallet_share > 0 and http_client_mod:
            await transfer_money(
                http_client_mod,
                int(wallet_share),
                "Party Share",
                str(other_char.player.unique_id),
            )

    # 5. Apply shortcut zone: zero out subsidy after bonus was factored in
    if used_shortcut:
        share_subsidy = 0

    # 6. Build profit tuples for all members
    # Earner keeps the integer division remainder via their own share calculation:
    # earner gets: total - (share * (party_size - 1))
    earner_base = total_base_payment - share_base * len(other_characters)
    earner_contract = total_contract_payment - share_contract * len(other_characters)
    if used_shortcut:
        earner_subsidy = 0
    else:
        earner_subsidy = total_subsidy - share_subsidy * len(other_characters)

    result = [(character, earner_subsidy, earner_base, earner_contract)]
    for other_char in other_characters:
        result.append((other_char, share_subsidy, share_base, share_contract))
    return result


LAST_SEQ_CACHE_KEY = "webhook:last_processed_seq"
LAST_TS_CACHE_KEY = "webhook:last_processed_ts"
LAST_EPOCH_CACHE_KEY = "webhook:last_epoch"


async def process_events(
    events, http_client=None, http_client_mod=None, discord_client=None
):
    # ── Epoch-based reset ──
    # If the game server restarted, events will carry a new _epoch.
    # Detect this and reset the seq high-water mark so we don't
    # silently drop events with lower seq numbers from the new session.
    last_processed = cache.get(LAST_SEQ_CACHE_KEY, 0)
    cached_epoch = cache.get(LAST_EPOCH_CACHE_KEY)
    for event in events:
        event_epoch = event.get("_epoch")
        if event_epoch is not None:
            if cached_epoch is not None and event_epoch != cached_epoch:
                logger.warning(
                    "Epoch changed: %s -> %s (server restarted), resetting seq high-water mark",
                    cached_epoch, event_epoch,
                )
                last_processed = 0
                cache.set(LAST_SEQ_CACHE_KEY, 0, timeout=None)
            elif cached_epoch is None:
                # First SSE connection — LAST_SEQ may be stale from old
                # polling system.  Reset to avoid dropping all events.
                last_processed = 0
                cache.set(LAST_SEQ_CACHE_KEY, 0, timeout=None)
            if event_epoch != cached_epoch:
                cached_epoch = event_epoch
                cache.set(LAST_EPOCH_CACHE_KEY, cached_epoch, timeout=None)
            break  # Only need to check one event for epoch

    # ── Seq-based deduplication ──
    # Filter out events we've already processed, using the monotonic _seq
    # assigned by the C++ EventManager.
    new_events = []
    max_seq = last_processed
    for event in events:
        seq = event.get("_seq")
        if seq is not None:
            if seq <= last_processed:
                continue  # Already processed
            max_seq = max(max_seq, seq)
        new_events.append(event)
    events = new_events

    # ── Timestamp-floor deduplication (pre-sequence hotfix) ──
    # For events without _seq (old mod), skip if timestamp <= last processed.
    # This prevents full buffer replay on worker restart.
    last_processed_ts = cache.get(LAST_TS_CACHE_KEY, 0)
    if last_processed_ts:
        events = [
            e for e in events
            if e.get("_seq") is not None or e["timestamp"] > last_processed_ts
        ]

    if not events:
        return

    # Pre-process events to simplify keys
    for event in events:
        player_id = event["data"].get("CharacterGuid", "")
        if not player_id:
            player_id = event["data"].get("PlayerId", "")
        event["key_id"] = player_id

    def key_fn(event):
        return (event["key_id"], event["hook"])

    sorted_events = sorted(events, key=key_fn)
    aggregated_events = aggregate_homogenous_events(sorted_events)

    def key_by_character(event):
        player_id = event["data"].get("CharacterGuid", "")
        if not player_id:
            player_id = event["data"].get("PlayerId", "")
        return player_id

    sorted_player_events = sorted(aggregated_events, key=key_by_character)
    grouped_player_events = itertools.groupby(
        sorted_player_events, key=key_by_character
    )

    player_profits = []

    treasury_balance = await get_treasury_fund_balance()
    parties = await get_parties(http_client_mod) if (PARTY_BONUS_ENABLED and http_client_mod) else []
    active_term = await MinistryTerm.objects.filter(is_active=True).afirst()
    for character_guid, es in grouped_player_events:
        if not character_guid:
            continue

        try:
            character_q = Q(guid=character_guid, guid__isnull=False)
            try:
                character_q = character_q | Q(player__unique_id=int(character_guid))
            except ValueError:
                pass

            character = await (
                Character.objects.select_related("player")
                .with_last_login()
                .filter(character_q)
                .order_by("-last_login")
                .afirst()
            )
            if not character:
                continue
            player = character.player
        except Player.DoesNotExist:
            continue

        total_base_payment = 0
        total_subsidy = 0
        total_contract_payment = 0

        is_rp_mode = await get_rp_mode(http_client_mod, character_guid)
        used_shortcut = (
            character.shortcut_zone_entered_at is not None
            and character.shortcut_zone_entered_at > timezone.now() - timedelta(hours=1)
        )

        for event in es:
            try:
                base_pay, subsidy, contract_pay = await process_event(
                    event,
                    player,
                    character,
                    is_rp_mode,
                    used_shortcut,
                    treasury_balance,
                    http_client,
                    http_client_mod,
                    discord_client,
                    active_term=active_term,
                )
                total_base_payment += base_pay
                total_subsidy += subsidy
                total_contract_payment += contract_pay
            except Exception as e:
                event_str = json.dumps(event)
                asyncio.create_task(
                    show_popup(
                        http_client_mod,
                        f"Webhook failed, please send to discord:\n{e}\n{event_str}",
                        character_guid=character.guid,
                    )
                )
                raise e

        # Party bonus + payment splitting
        party_result = await split_party_payment(
            character, parties,
            total_base_payment, total_subsidy, total_contract_payment,
            http_client_mod, used_shortcut=used_shortcut,
        )
        if party_result is not None:
            player_profits.extend(party_result)
            if used_shortcut:
                await Character.objects.filter(pk=character.pk).aupdate(
                    shortcut_zone_entered_at=None
                )
            continue

        # Solo path: shortcut zones zero out subsidy
        if used_shortcut:
            total_subsidy = 0
            await Character.objects.filter(pk=character.pk).aupdate(
                shortcut_zone_entered_at=None
            )

        player_profits.append((character, total_subsidy, total_base_payment, total_contract_payment))

    if http_client_mod:
        await on_player_profits(player_profits, http_client_mod, http_client)

    # Persist high-water marks after successful processing
    if max_seq > last_processed:
        cache.set(LAST_SEQ_CACHE_KEY, max_seq, timeout=None)
    # Timestamp floor for events without _seq
    max_ts = max((e["timestamp"] for e in events if e.get("_seq") is None), default=0)
    if max_ts and max_ts > last_processed_ts:
        cache.set(LAST_TS_CACHE_KEY, max_ts, timeout=None)


async def process_cargo_log(cargo, player, character, timestamp):
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


def atomic_process_delivery(job_id, quantity, delivery_data):
    """
    atomically updates the job and creates the delivery log
    """
    from amc.models import DeliveryJob  # import here to avoid circular if any

    with transaction.atomic():
        job = None
        quantity_to_add = 0
        if job_id:
            job = DeliveryJob.objects.select_for_update().get(pk=job_id)
            requested_remaining = job.quantity_requested - job.quantity_fulfilled
            quantity_to_add = min(requested_remaining, quantity)
            if quantity_to_add > 0:
                job.quantity_fulfilled = cast(
                    Any, F("quantity_fulfilled") + quantity_to_add
                )
                job.save(update_fields=["quantity_fulfilled"])
                job.refresh_from_db(fields=["quantity_fulfilled"])

        bonus = 0
        if job and quantity_to_add > 0:
            # Correct multiplier logic: (multiplier - 1) * portion_of_payment
            # portion_of_payment = (quantity_to_add / delivery_data['quantity']) * delivery_data['payment']
            # but usually quantity_to_add == delivery_data['quantity']

            multiplier = max(0, job.bonus_multiplier)
            bonus = int(
                delivery_data["payment"]
                * (quantity_to_add / delivery_data["quantity"])
                * multiplier
                + 0.5
            )
            delivery_data["subsidy"] += bonus

        Delivery.objects.create(job=job, **delivery_data)
        return job


async def handle_load_cargo(event, character, player, http_client_mod, http_client=None):
    cargo = event["data"].get("Cargo", {})
    if cargo.get("Net_CargoKey") != "Money":
        return

    # --- Throttled smuggling tip-off announcement ---
    if SMUGGLING_TIPOFF_ENABLED and http_client:
        tipoff_cache_key = f"smuggling_tipoff:{character.guid}"
        already_tipped = await cache.aget(tipoff_cache_key)
        if not already_tipped:
            await cache.aset(tipoff_cache_key, True, timeout=SMUGGLING_TIPOFF_COOLDOWN)
            asyncio.create_task(
                _announce_smuggling_tipoff_after_delay(
                    http_client, delay=SMUGGLING_TIPOFF_DELAY,
                )
            )

    try:
        vehicles = await list_player_vehicles(http_client_mod, str(player.unique_id), active=True, complete=True)
        if not vehicles:
            return
        
        main_vehicle = next((v for v in vehicles.values() if v.get("isLastVehicle") and v.get("index", -1) == 0), None)
        if not main_vehicle:
            return
        
        # Whitelist police parts for officers on active duty
        whitelist = None
        is_on_duty = await PoliceSession.objects.filter(
            character=character, ended_at__isnull=True
        ).aexists()
        if is_on_duty:
            whitelist = POLICE_DUTY_WHITELIST
        custom_parts = detect_custom_parts(main_vehicle.get("parts", []), whitelist=whitelist)
        if custom_parts:
            asyncio.create_task(
                show_popup(
                    http_client_mod,
                    "You are not allowed to use modified vehicles for criminal gameplay!",
                    character_guid=character.guid,
                    player_id=str(player.unique_id),
                )
            )
    except Exception as e:
        logger.warning(f"Failed to check custom parts for load cargo: {e}")


async def process_event(
    event,
    player,
    character,
    is_rp_mode=False,
    used_shortcut=False,
    treasury_balance=None,
    http_client=None,
    http_client_mod=None,
    discord_client=None,
    active_term=None,
):
    """Process a single webhook event.

    Returns:
        (base_payment, subsidy, contract_payment) — all kept separate,
        never baked together. base_payment is what the game deposited
        into the player's wallet. subsidy will be paid separately.
    """
    print(event)
    base_payment = 0
    subsidy = 0
    contract_payment = 0
    current_tz = timezone.get_current_timezone()
    timestamp = timezone.datetime.fromtimestamp(event["timestamp"], tz=current_tz)

    match event["hook"]:
        case "ServerCargoArrived":
            payment, sub = await handle_cargo_arrived(
                event,
                player,
                character,
                timestamp,
                treasury_balance,
                is_rp_mode,
                used_shortcut,
                http_client,
                http_client_mod=http_client_mod,
                discord_client=discord_client,
                active_term=active_term,
            )
            base_payment += payment
            subsidy += sub

        case "ServerCargoDumped":
            payment, sub = await handle_cargo_dumped(event, player, timestamp)
            base_payment += payment
            subsidy += sub

        case "ServerSignContract":
            await handle_contract_signed(event, player, timestamp)

        case "ServerContractCargoDelivered":
            payment, _ = await handle_contract_delivered(event, player, timestamp)
            contract_payment += payment

        case "ServerPassengerArrived":
            payment, sub = await handle_passenger_arrived(
                event, player, timestamp,
                character=character, http_client_mod=http_client_mod,
            )
            base_payment += payment
            subsidy += sub

        case "ServerTowRequestArrived":
            payment, sub = await handle_tow_request(event, player, timestamp)
            base_payment += payment
            subsidy += sub

        case "ServerResetVehicleAt":
            await handle_reset_vehicle(character, timestamp, is_rp_mode, http_client)

        case "ServerTeleportCharacter" | "ServerTeleportVehicle" | "ServerRespawnCharacter":
            await handle_teleport_or_respawn(event, character, timestamp, http_client_mod, http_client)

        case "ServerArrivedAtPolicePatrolPoint":
            await handle_patrol_arrived(event, player, timestamp, http_client_mod)

        case "ServerSelectPolicePullOverPenaltyResponse":
            await handle_police_penalty(event, player, timestamp)

        case "ServerAddPolicePlayer":
            await handle_police_shift(event, player, timestamp, PoliceShiftLog.Action.START)

        case "ServerRemovePolicePlayer":
            await handle_police_shift(event, player, timestamp, PoliceShiftLog.Action.END)

        case "ServerPickupCargo":
            await handle_pickup_cargo(event, character, http_client, http_client_mod)

        case "ServerLoadCargo":
            if http_client_mod:
                await handle_load_cargo(event, character, player, http_client_mod, http_client=http_client)

    return base_payment, subsidy, contract_payment
