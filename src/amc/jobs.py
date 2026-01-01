import math
import asyncio
import random
from typing import List
import itertools
from operator import attrgetter
from datetime import timedelta
from django.utils import timezone
from django.db.models import Q, Prefetch
from amc.models import (
    Cargo,
    DeliveryPointStorage,
    DeliveryJob,
    DeliveryJobTemplate,
    MinistryTerm,
    Delivery,
    Character,
)
from amc.game_server import get_players, announce
from amc_finance.services import (
    get_treasury_fund_balance,
    escrow_ministry_funds,
    process_ministry_expiration,
    send_fund_to_player,
    process_ministry_completion,
    process_treasury_expiration_penalty,
)


async def get_job_success_rate(hours_lookback: int = 24) -> tuple[float, int, int]:
    """
    Calculate job completion rate over recent history.
    Returns: (success_rate, completed_count, expired_count)
    """
    cutoff = timezone.now() - timedelta(hours=hours_lookback)

    completed = await DeliveryJob.objects.filter(
        fulfilled_at__gte=cutoff,
        fulfilled_at__isnull=False,
    ).acount()

    expired = await DeliveryJob.objects.filter(
        expired_at__gte=cutoff,
        expired_at__lt=timezone.now(),
        fulfilled_at__isnull=True,
    ).acount()

    total = completed + expired
    if total == 0:
        return (1.0, 0, 0)  # No data, assume healthy

    return (completed / total, completed, expired)


def calculate_adaptive_multiplier(
    success_rate: float,
    target_rate: float = 0.80,
    min_mult: float = 0.5,
    max_mult: float = 2.0,
) -> float:
    """
    Returns multiplier for max_active_jobs based on success rate.
    - If success_rate > target: multiplier > 1 (post more jobs)
    - If success_rate < target: multiplier < 1 (post fewer jobs)
    """
    if success_rate >= target_rate:
        # Scale up: 80% → 1.0x, 100% → 2.0x
        ratio = (success_rate - target_rate) / (1.0 - target_rate)
        multiplier = 1.0 + ratio * (max_mult - 1.0)
    else:
        # Scale down: 80% → 1.0x, 0% → 0.5x
        ratio = success_rate / target_rate
        multiplier = min_mult + ratio * (1.0 - min_mult)

    return max(min_mult, min(max_mult, multiplier))


async def cleanup_expired_jobs():
    # 1. Handle Ministry-funded expired jobs (existing)
    expired_jobs = DeliveryJob.objects.filter(
        expired_at__lt=timezone.now(),
        fulfilled_at__isnull=True,
        funding_term__isnull=False,
        escrowed_amount__gt=0,
    )
    async for job in expired_jobs:
        await process_ministry_expiration(job)

    # 2. Handle non-Ministry expired jobs (government shutdown)
    # Treasury pays penalty of 50% of completion_bonus
    expired_non_ministry_jobs = DeliveryJob.objects.filter(
        expired_at__lt=timezone.now(),
        fulfilled_at__isnull=True,
        funding_term__isnull=True,
        completion_bonus__gt=0,
    )
    async for job in expired_non_ministry_jobs:
        await process_treasury_expiration_penalty(job)


async def monitor_jobs(ctx):
    await cleanup_expired_jobs()
    num_active_jobs = await DeliveryJob.objects.filter_active().acount()
    players = await get_players(ctx["http_client"])
    num_players = len(players)
    treasury_balance = await get_treasury_fund_balance()
    treasury_health = min(1.0, float(treasury_balance) / 50_000_000)

    # Get adaptive multiplier from recent history
    success_rate, _, _ = await get_job_success_rate(hours_lookback=24)
    adaptive_mult = calculate_adaptive_multiplier(success_rate)

    # Base formula: 1 job per 10 players, floor of 2
    base_max_jobs = max(2, 1 + math.ceil(num_players / 10))
    max_active_jobs = max(1, int(base_max_jobs * adaptive_mult))

    if num_active_jobs >= max_active_jobs:
        return

    job_templates = (
        DeliveryJobTemplate.objects.exclude_has_conflicting_active_job()
        .exclude_recently_posted()
        .prefetch_related(
            Prefetch("cargos", queryset=Cargo.objects.select_related("type").all()),
            "source_points",
            "destination_points",
        )
        .order_by("?")
    )

    async for template in job_templates:
        cargos = template.cargos.all()
        source_points = template.source_points.all()
        destination_points = template.destination_points.all()

        non_type_cargos = [c for c in cargos if "T::" not in c.key]
        destination_storages = DeliveryPointStorage.objects.filter(
            Q(cargo__in=non_type_cargos) | Q(cargo__type__in=cargos),
            delivery_point__in=destination_points,
        ).annotate_default_capacity()
        source_storages = DeliveryPointStorage.objects.filter(
            Q(cargo__in=non_type_cargos) | Q(cargo__type__in=cargos),
            delivery_point__in=source_points,
        ).annotate_default_capacity()

        destination_storage_capacities = [
            (storage.amount, storage.capacity_normalized or 0)
            async for storage in destination_storages
        ]
        source_storage_capacities = [
            (storage.amount, storage.capacity_normalized or 0)
            async for storage in source_storages
        ]
        destination_amount = sum(
            [amount for amount, capacity in destination_storage_capacities]
        )
        destination_capacity = sum(
            [capacity for amount, capacity in destination_storage_capacities]
        )
        source_amount = sum([amount for amount, capacity in source_storage_capacities])
        source_capacity = sum(
            [capacity for amount, capacity in source_storage_capacities]
        )

        quantity_requested = template.default_quantity
        if template.expected_player_count_for_quantity:
            quantity_requested = min(
                quantity_requested,
                int(
                    quantity_requested
                    * num_players
                    / template.expected_player_count_for_quantity
                ),
            )

        if destination_capacity == 0:
            is_destination_empty = True
        else:
            is_destination_empty = (
                (destination_amount / destination_capacity) <= 0.15
            ) or (destination_capacity - destination_amount >= quantity_requested)

        if destination_capacity > 0:
            quantity_requested = min(
                quantity_requested, destination_capacity - destination_amount
            )

        if source_capacity == 0:
            is_source_enough = True
        elif source_amount >= source_capacity * 0.85:
            is_source_enough = True
        else:
            is_source_enough = source_amount >= quantity_requested

        if not is_destination_empty or not is_source_enough:
            continue

        chance = (
            template.job_posting_probability
            * max(10, num_players)
            / 2000
            / (5 + num_active_jobs * 2)
        )
        if not source_points and not destination_points:
            chance = chance / (24 * 3)

        if random.random() > chance:
            continue

        rp_mode = template.rp_mode or random.random() < 0.15
        bonus_multiplier = round(
            template.bonus_multiplier * random.uniform(0.8, 1.2), 2
        )
        bonus_multiplier = bonus_multiplier * treasury_health
        completion_bonus = int(
            template.completion_bonus
            * quantity_requested
            / template.default_quantity
            * random.uniform(0.7, 1.3)
        )
        completion_bonus = int(treasury_health * completion_bonus)
        if rp_mode and not template.rp_mode:
            completion_bonus = completion_bonus * 1.5

        active_term = await MinistryTerm.objects.filter(is_active=True).afirst()
        if active_term:
            # Check if Ministry has enough budget
            # Ideally we should strictly check account balance, but current_budget is a good proxy for speed
            if active_term.current_budget < completion_bonus:
                continue  # Skip this job if budget is exhausted

        new_job = await DeliveryJob.objects.acreate(
            name=template.name,
            quantity_requested=quantity_requested,
            expired_at=timezone.now() + timedelta(hours=template.duration_hours),
            bonus_multiplier=bonus_multiplier,
            completion_bonus=completion_bonus,
            description=template.description,
            rp_mode=rp_mode,
            created_from=template,
            funding_term=active_term,
        )

        if active_term:
            # Escrow funds and update job
            if await escrow_ministry_funds(completion_bonus, new_job):
                new_job.escrowed_amount = completion_bonus
                await new_job.asave()
            else:
                # Failed to escrow (race condition?), delete job or leave as unfunded?
                # Safest is to delete or mark invalid, but for now let's just log/ignore as it's rare.
                pass

        await new_job.cargos.aadd(*cargos)
        await new_job.source_points.aadd(*source_points)
        await new_job.destination_points.aadd(*destination_points)
        asyncio.create_task(
            announce(
                f"New job posting! {template.name} - {completion_bonus:,} bonus on completion. See /jobs for more details",
                ctx["http_client"],
            )
        )
        break


async def on_delivery_job_fulfilled(job, http_client):
    """
    Finds all players who contributed to a job and rewards them proportionally.
    """
    # Define a completion bonus. Defaults to 50,000 if not set on the job model.
    completion_bonus = getattr(job, "completion_bonus", 50_000)
    if completion_bonus == 0:
        return

    log_qs = Delivery.objects.filter(job=job).order_by("timestamp")

    # Get the exact N logs that fulfilled the job by taking the most recent ones.
    contributing_logs = []
    acc = job.quantity_requested
    async for log in log_qs:
        log.quantity = min(log.quantity, acc)
        acc = acc - log.quantity
        contributing_logs.append(log)

    total_deliveries = job.quantity_fulfilled
    if not total_deliveries:
        return

    # Group logs by player to count each player's contribution.
    contributing_logs.sort(key=attrgetter("character_id"))
    character_contributions = {}
    for character_id, group in itertools.groupby(
        contributing_logs, key=attrgetter("character_id")
    ):
        if character_id:
            character_deliveries = list(group)
            character_contributions[character_id] = {
                "count": sum([delivery.quantity for delivery in character_deliveries]),
                "reward": sum(
                    [
                        int(delivery.quantity / total_deliveries * completion_bonus)
                        for delivery in character_deliveries
                    ]
                ),
            }

    if not character_contributions:
        return

    # Fetch all contributing Player objects in one query.
    character_ids = character_contributions.keys()
    characters = {c.id: c async for c in Character.objects.filter(id__in=character_ids)}

    # Distribute the bonus proportionally.
    contributors_names: List[str] = []
    for character_id, character_contribution in character_contributions.items():
        character_obj = characters.get(character_id)
        if not character_obj:
            continue
        count = character_contribution["count"]
        reward = character_contribution["reward"]
        if reward > 0:
            await send_fund_to_player(reward, character_obj, "Job Completion")
            contributors_names.append(f"{character_obj.name} ({count})")

    contributors_str = ", ".join(contributors_names)
    message = f'"{job.name}" Completed! +${completion_bonus:,} has been deposited into your bank accounts. Thanks to: {contributors_str}'

    # Ministry Rebate Logic
    if job.funding_term_id:
        await process_ministry_completion(job, completion_bonus)

    asyncio.create_task(announce(message, http_client, color="90EE90"))
