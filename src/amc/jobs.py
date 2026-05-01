import math
import asyncio
import random
from typing import List
import itertools
from operator import attrgetter
from datetime import timedelta
from django.utils import timezone
from django.db.models import Q, F, Prefetch
from django.db.models.functions import Least, Greatest
from amc.models import (
    Cargo,
    DeliveryPointStorage,
    DeliveryJob,
    DeliveryJobTemplate,
    MinistryTerm,
    Delivery,
    Character,
    JobPostingConfig,
)
from amc.game_server import get_players, announce
from amc import config
from amc_finance.services import (
    get_treasury_fund_balance,
    escrow_ministry_funds,
    process_ministry_expiration,
    send_fund_to_player,
    process_ministry_completion,
    process_treasury_expiration_penalty,
)


async def _decay_template_score(job):
    """Decay the template's success_score on job expiry."""
    if job.created_from_id:
        await DeliveryJobTemplate.objects.filter(pk=job.created_from_id).aupdate(
            success_score=Greatest(0.1, F("success_score") * 0.70),
            lifetime_expirations=F("lifetime_expirations") + 1,
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
    target_rate: float = 0.50,
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


async def cleanup_expired_jobs(http_client):
    # 1. Handle Ministry-funded expired jobs (existing)
    expired_jobs = DeliveryJob.objects.filter(
        expired_at__lt=timezone.now(),
        fulfilled_at__isnull=True,
        funding_term__isnull=False,
        escrowed_amount__gt=0,
    ).select_related("created_from")
    async for job in expired_jobs:
        ## await process_player_job_contribution(job)
        await process_ministry_expiration(job)
        await _decay_template_score(job)

    # 2. Handle non-Ministry expired jobs (government shutdown)
    # Treasury pays penalty of 50% of completion_bonus
    expired_non_ministry_jobs = DeliveryJob.objects.filter(
        expired_at__lt=timezone.now(),
        fulfilled_at__isnull=True,
        funding_term__isnull=True,
        completion_bonus__gt=0,
    ).select_related("created_from")
    async for job in expired_non_ministry_jobs:
        await payout_partial_contributors(job, http_client)
        await process_treasury_expiration_penalty(job)
        await _decay_template_score(job)


def calculate_treasury_multiplier(
    balance: float,
    equilibrium: float = 100_000_000,
    sensitivity: float = 1.5,
    cap_ratio: float = 4.0,
) -> float:
    """
    Asymmetric treasury multiplier for self-correcting spending control.
    Returns 0.0+:
    - At equilibrium balance: 1.0 (normal spending)
    - Below equilibrium: ratio^sensitivity (steep pullback, preserves money)
    - Above equilibrium: 1 + log(ratio)/log(cap_ratio) (gentle growth, no hard stall)
    """
    if balance <= 0:
        return 0.0
    ratio = balance / max(equilibrium, 1)
    if ratio <= 1.0:
        return ratio ** sensitivity
    return 1.0 + math.log(ratio) / math.log(max(cap_ratio, 1.01))


def weighted_shuffle(templates: list, weight_fn) -> list:
    """Shuffle templates with weighted probability — higher-weight templates
    are more likely to appear earlier in the sequence."""
    weights = [weight_fn(t) for t in templates]
    result = []
    remaining = list(zip(templates, weights))
    while remaining:
        total = sum(w for _, w in remaining)
        if total <= 0:
            # All remaining weights are zero, append in random order
            items = [t for t, _ in remaining]
            random.shuffle(items)
            result.extend(items)
            break
        r = random.random() * total
        cumulative = 0
        for i, (t, w) in enumerate(remaining):
            cumulative += w
            if r <= cumulative:
                result.append(t)
                remaining.pop(i)
                break
    return result


async def monitor_jobs(ctx):
    await cleanup_expired_jobs(ctx["http_client"])
    config = await JobPostingConfig.aget_config()
    num_active_jobs = await DeliveryJob.objects.filter_active().acount()
    players = await get_players(ctx["http_client"])
    num_players = len(players)
    treasury_balance = await get_treasury_fund_balance()
    treasury_mult = calculate_treasury_multiplier(
        float(treasury_balance),
        equilibrium=float(config.treasury_equilibrium),
        sensitivity=float(config.treasury_sensitivity),
        cap_ratio=float(config.treasury_cap_ratio),
    )

    # Get adaptive multiplier from recent history
    success_rate, _, _ = await get_job_success_rate(hours_lookback=24)
    adaptive_mult = calculate_adaptive_multiplier(
        success_rate,
        target_rate=config.target_success_rate,
        min_mult=config.min_multiplier,
        max_mult=config.max_multiplier,
    )

    # Base formula: log2 curve — generous at low player counts, flattens at high
    # e.g. 0→1, 6→4, 10→4, 20→5, 30→6
    base_max_jobs = config.min_base_jobs + round(math.log2(1 + num_players))
    max_active_jobs = max(1, int(base_max_jobs * adaptive_mult))

    slots_to_fill = max_active_jobs - num_active_jobs
    if slots_to_fill <= 0:
        return

    # Rate-limit: don't post more than max_posts_per_tick per cron cycle
    slots_to_fill = min(slots_to_fill, config.max_posts_per_tick)

    # Probabilistic posting: each slot has posting_chance to actually post.
    # treasury_mult + posting_rate_multiplier control the rate.
    posting_chance = min(1.0, treasury_mult * config.posting_rate_multiplier)
    slots_to_fill = sum(
        1 for _ in range(slots_to_fill) if random.random() < posting_chance
    )
    if slots_to_fill <= 0:
        return

    job_templates = (
        DeliveryJobTemplate.objects.exclude_has_conflicting_active_job()
        .filter(rp_mode=False, enabled=True)
        .exclude_recently_posted()
        .prefetch_related(
            Prefetch("cargos", queryset=Cargo.objects.select_related("type").all()),
            "source_points",
            "destination_points",
        )
    )

    # Filter out templates that conflict with active/future supply chain events
    from amc.supply_chain import get_conflicting_cargo_keys

    sc_conflicts = await get_conflicting_cargo_keys()
    filtered_templates = []
    async for template in job_templates:
        template_cargos = [c.key for c in template.cargos.all()]
        template_dests = [dp.pk for dp in template.destination_points.all()]
        conflicting = False
        for ck in template_cargos:
            for did in template_dests:
                if (ck, did) in sc_conflicts or (ck, -1) in sc_conflicts:
                    conflicting = True
                    break
            if conflicting:
                break
        if not conflicting:
            filtered_templates.append(template)

    # Deduplicate templates (M2M prefetch can cause duplicates)
    seen_ids = set()
    unique_templates = []
    for t in filtered_templates:
        if t.pk not in seen_ids:
            seen_ids.add(t.pk)
            unique_templates.append(t)

    # Weighted shuffle: templates with higher probability × success_score
    # are more likely to be selected first
    ordered_templates = weighted_shuffle(
        unique_templates,
        lambda t: t.job_posting_probability * t.success_score,
    )

    active_term = await MinistryTerm.objects.filter(is_active=True).afirst()

    posted = 0
    # Track source amounts already claimed by jobs posted in this tick
    # to prevent multiple jobs from depleting the same source storage
    reserved_source: dict[
        tuple[int, int], int
    ] = {}  # (cargo_id, dp_id) -> reserved qty

    for template in ordered_templates:
        if posted >= slots_to_fill:
            break

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
        # Collect source storages with their IDs for reservation tracking
        source_storage_entries = [
            (
                storage.cargo_id,
                storage.delivery_point_id,
                storage.amount,
                storage.capacity_normalized or 0,
            )
            async for storage in source_storages
        ]
        destination_amount = sum(
            [amount for amount, capacity in destination_storage_capacities]
        )
        destination_capacity = sum(
            [capacity for amount, capacity in destination_storage_capacities]
        )
        # Subtract amounts already reserved by earlier jobs in this tick
        source_amount = sum(
            max(0, amount - reserved_source.get((cargo_id, dp_id), 0))
            for cargo_id, dp_id, amount, capacity in source_storage_entries
        )
        source_capacity = sum(
            capacity for cargo_id, dp_id, amount, capacity in source_storage_entries
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
                (destination_amount / destination_capacity) <= 0.3
            ) or (destination_capacity - destination_amount >= quantity_requested)

        if destination_capacity > 0:
            quantity_requested = min(
                quantity_requested, destination_capacity - destination_amount
            )

        if quantity_requested <= 0:
            continue

        if source_capacity == 0:
            is_source_enough = True
        elif source_amount >= source_capacity * 0.85:
            is_source_enough = True
        else:
            is_source_enough = source_amount >= quantity_requested

        if not is_destination_empty or not is_source_enough:
            continue

        # Treasury multiplier influences bonus amounts (Bonus multiplier)
        bonus_multiplier = round(
            template.bonus_multiplier * random.uniform(0.95, 1.05), 2 # 5% random variance
        )
        bonus_multiplier = bonus_multiplier * treasury_mult
        base_bonus = int(
            template.completion_bonus * quantity_requested / template.default_quantity
        )
        scaling_factor = max(treasury_mult * 0.5, min(2.0, treasury_mult * random.uniform(0.95, 1.1)))
        completion_bonus = int(base_bonus * scaling_factor)

        if active_term:
            # Check if Ministry has enough budget
            if active_term.current_budget < completion_bonus:
                continue  # Skip this job if budget is exhausted

        duration_hours = template.duration_hours

        new_job = await DeliveryJob.objects.acreate(
            name=template.name,
            quantity_requested=quantity_requested,
            expired_at=timezone.now() + timedelta(hours=duration_hours),
            bonus_multiplier=bonus_multiplier,
            completion_bonus=completion_bonus,
            description=template.description,
            rp_mode=False,
            created_from=template,
            funding_term=active_term,
        )

        if active_term:
            # Escrow funds and update job
            if await escrow_ministry_funds(completion_bonus, new_job):
                new_job.escrowed_amount = completion_bonus
                await new_job.asave()
            else:
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
        posted += 1

        # Reserve source amounts so subsequent jobs see reduced availability
        for cargo_id, dp_id, amount, capacity in source_storage_entries:
            key = (cargo_id, dp_id)
            reserved_source[key] = reserved_source.get(key, 0) + quantity_requested


def get_cargo_fulfillment_weight(cargo_key: str | None) -> int:
    if not cargo_key:
        return 1
    return config.CARGO_FULFILLMENT_WEIGHTS.get(cargo_key, 1)


async def payout_partial_contributors(job, http_client):
    """
    Finds all players who contributed to a job and rewards them proportionally.
    """
    # Define a completion bonus. Defaults to 50,000 if not set on the job model.
    completion_bonus = getattr(job, "completion_bonus", 50_000)
    if completion_bonus <= 0:
        return

    if job.quantity_requested <= 0 or job.quantity_fulfilled <= 0:
        return

    ratio = min(1.0, job.quantity_fulfilled / job.quantity_requested)
    partial_bonus = int(completion_bonus * ratio)
    if partial_bonus <= 0:
        return

    log_qs = Delivery.objects.filter(job=job).order_by("timestamp")

    # Cap each delivery to what actually counted toward fulfillment.
    # `quantity_requested` and `quantity_fulfilled` are in *weighted* units, see amc.config.CARGO_FULFILLMENT_WEIGHTS
    contributing_logs = []
    qr = job.quantity_requested
    async for log in log_qs:
        if qr <= 0:
            break
        weight = get_cargo_fulfillment_weight(log.cargo_key)
        weighted = log.quantity * weight
        weighted = min(weighted, qr)
        # Store the weighted contribution back on log.quantity for the
        # downstream sum/groupby — this is a transient in-memory mutation,
        # not persisted.
        log.quantity = weighted
        qr = qr - weighted
        contributing_logs.append(log)

    total_deliveries = job.quantity_fulfilled

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
                        int(delivery.quantity / total_deliveries * partial_bonus)
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
            if character_obj.is_gov_employee:
                from amc.gov_employee import redirect_income_to_treasury

                # Job bonus comes from treasury's own escrowed funds,
                # no real money moves. Only track as contribution for levels.
                await redirect_income_to_treasury(
                    0,
                    character_obj,
                    "Government Service – Job Bonus",
                    http_client=http_client,
                    contribution=reward,
                )
            else:
                await send_fund_to_player(reward, character_obj, "Job Completion")
            contributors_names.append(f"{character_obj.name} ({count})")

    if not contributors_names:
        return

    contributors_str = ", ".join(contributors_names)
    message = f'"{job.name}" expired at {ratio * 100:.0f}% completion. +${partial_bonus:,} has been deposited into your bank accounts. Thanks to: {contributors_str}'

    # Ministry Rebate Logic
    if job.funding_term_id:
        await process_ministry_completion(job, completion_bonus)

    # Boost template success score on completion
    if job.created_from_id:
        await DeliveryJobTemplate.objects.filter(pk=job.created_from_id).aupdate(
            success_score=Least(2.0, F("success_score") * 1.15),
            lifetime_completions=F("lifetime_completions") + 1,
        )

    asyncio.create_task(announce(message, http_client, color="90EE90"))

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
    # `quantity_requested` / `quantity_fulfilled` are in *weighted* units
    # (see amc.config.CARGO_FULFILLMENT_WEIGHTS), so weigh each log here too.
    contributing_logs = []
    acc = job.quantity_requested
    async for log in log_qs:
        weight = get_cargo_fulfillment_weight(log.cargo_key)
        weighted = log.quantity * weight
        weighted = min(weighted, acc)
        log.quantity = weighted
        acc = acc - weighted
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
            if character_obj.is_gov_employee:
                from amc.gov_employee import redirect_income_to_treasury

                # Job bonus comes from treasury's own escrowed funds,
                # no real money moves. Only track as contribution for levels.
                await redirect_income_to_treasury(
                    0,
                    character_obj,
                    "Government Service – Job Bonus",
                    http_client=http_client,
                    contribution=reward,
                )
            else:
                await send_fund_to_player(reward, character_obj, "Job Completion")
            contributors_names.append(f"{character_obj.name} ({count})")

    contributors_str = ", ".join(contributors_names)
    message = f'"{job.name}" Completed! +${completion_bonus:,} has been deposited into your bank accounts. Thanks to: {contributors_str}'

    # Ministry Rebate Logic
    if job.funding_term_id:
        await process_ministry_completion(job, completion_bonus)

    # Boost template success score on completion
    if job.created_from_id:
        await DeliveryJobTemplate.objects.filter(pk=job.created_from_id).aupdate(
            success_score=Least(2.0, F("success_score") * 1.15),
            lifetime_completions=F("lifetime_completions") + 1,
        )

    asyncio.create_task(announce(message, http_client, color="90EE90"))

