"""Internal economy management API — SubsidyRule CRUD and JobPostingConfig.

Only accessible within the Tailscale network (asean-mt-server:9000).
No additional auth required — network isolation is the security boundary.
"""

from django.http import HttpRequest
from ninja import Router, Schema

from amc.models import (
    Cargo,
    DeliveryPoint,
    JobPostingConfig,
    SubsidyArea,
    SubsidyRule,
)

router = Router()


# ── Schemas ────────────────────────────────────────────────────────────


class SubsidyRuleDetailSchema(Schema):
    id: int
    name: str
    active: bool
    priority: int
    reward_type: str
    reward_value: float
    scales_with_damage: bool
    requires_on_time: bool
    allocation: float
    spent: float
    cargo_keys: list[str]
    source_area_names: list[str]
    destination_area_names: list[str]
    source_delivery_point_guids: list[str]
    destination_delivery_point_guids: list[str]


class SubsidyRuleCreateRequest(Schema):
    name: str
    reward_type: str
    reward_value: float
    priority: int = 0
    active: bool = True
    scales_with_damage: bool = False
    requires_on_time: bool = False
    allocation: float = 0
    cargo_keys: list[str] = []
    source_area_names: list[str] = []
    destination_area_names: list[str] = []
    source_delivery_point_guids: list[str] = []
    destination_delivery_point_guids: list[str] = []


class SubsidyRuleUpdateRequest(Schema):
    name: str | None = None
    active: bool | None = None
    priority: int | None = None
    reward_type: str | None = None
    reward_value: float | None = None
    scales_with_damage: bool | None = None
    requires_on_time: bool | None = None
    allocation: float | None = None
    cargo_keys: list[str] | None = None
    source_area_names: list[str] | None = None
    destination_area_names: list[str] | None = None
    source_delivery_point_guids: list[str] | None = None
    destination_delivery_point_guids: list[str] | None = None


class SubsidyRuleReorderRequest(Schema):
    ordered_ids: list[int]


class JobConfigSchema(Schema):
    target_success_rate: float
    min_multiplier: float
    max_multiplier: float
    players_per_job: int
    min_base_jobs: int
    min_active_jobs: int
    posting_rate_multiplier: float
    treasury_equilibrium: int
    treasury_sensitivity: float
    treasury_cap_ratio: float
    max_posts_per_tick: int


class JobConfigUpdateRequest(Schema):
    target_success_rate: float | None = None
    min_multiplier: float | None = None
    max_multiplier: float | None = None
    players_per_job: int | None = None
    min_base_jobs: int | None = None
    min_active_jobs: int | None = None
    posting_rate_multiplier: float | None = None
    treasury_equilibrium: int | None = None
    treasury_sensitivity: float | None = None
    treasury_cap_ratio: float | None = None
    max_posts_per_tick: int | None = None


class ErrorSchema(Schema):
    error: str


class SuccessSchema(Schema):
    success: bool
    message: str


# ── Helpers ────────────────────────────────────────────────────────────


async def _serialize_rule(rule: SubsidyRule) -> dict:
    return {
        "id": rule.id,
        "name": rule.name,
        "active": rule.active,
        "priority": rule.priority,
        "reward_type": rule.reward_type,
        "reward_value": float(rule.reward_value),
        "scales_with_damage": rule.scales_with_damage,
        "requires_on_time": rule.requires_on_time,
        "allocation": float(rule.allocation),
        "spent": float(rule.spent),
        "cargo_keys": [c.key async for c in rule.cargos.all()],
        "source_area_names": [a.name async for a in rule.source_areas.all()],
        "destination_area_names": [
            a.name async for a in rule.destination_areas.all()
        ],
        "source_delivery_point_guids": [
            dp.guid async for dp in rule.source_delivery_points.all()
        ],
        "destination_delivery_point_guids": [
            dp.guid async for dp in rule.destination_delivery_points.all()
        ],
    }


async def _resolve_m2m_fields(
    cargo_keys: list[str] | None,
    source_area_names: list[str] | None,
    destination_area_names: list[str] | None,
    source_dp_guids: list[str] | None,
    destination_dp_guids: list[str] | None,
) -> dict:
    result = {}
    if cargo_keys is not None:
        result["cargos"] = [
            c async for c in Cargo.objects.filter(key__in=cargo_keys)
        ]
    if source_area_names is not None:
        result["source_areas"] = [
            a async for a in SubsidyArea.objects.filter(name__in=source_area_names)
        ]
    if destination_area_names is not None:
        result["destination_areas"] = [
            a async for a in SubsidyArea.objects.filter(name__in=destination_area_names)
        ]
    if source_dp_guids is not None:
        result["source_delivery_points"] = [
            dp async for dp in DeliveryPoint.objects.filter(guid__in=source_dp_guids)
        ]
    if destination_dp_guids is not None:
        result["destination_delivery_points"] = [
            dp async for dp in DeliveryPoint.objects.filter(guid__in=destination_dp_guids)
        ]
    return result


# ── SubsidyRule endpoints ──────────────────────────────────────────────


@router.get(
    "/subsidy-rules/",
    response=list[SubsidyRuleDetailSchema],
)
async def list_subsidy_rules(request: HttpRequest):
    rules = SubsidyRule.objects.prefetch_related(
        "cargos",
        "source_areas",
        "destination_areas",
        "source_delivery_points",
        "destination_delivery_points",
    ).order_by("-priority")
    return [await _serialize_rule(rule) async for rule in rules]


@router.post(
    "/subsidy-rules/",
    response={201: SubsidyRuleDetailSchema, 400: ErrorSchema},
)
async def create_subsidy_rule(
    request: HttpRequest, payload: SubsidyRuleCreateRequest
):
    if payload.reward_type not in ("PERCENTAGE", "FLAT"):
        return 400, {"error": "reward_type must be 'PERCENTAGE' or 'FLAT'"}
    if payload.reward_value <= 0:
        return 400, {"error": "reward_value must be positive"}

    rule = await SubsidyRule.objects.acreate(
        name=payload.name,
        reward_type=payload.reward_type,
        reward_value=payload.reward_value,
        priority=payload.priority,
        active=payload.active,
        scales_with_damage=payload.scales_with_damage,
        requires_on_time=payload.requires_on_time,
        allocation=payload.allocation,
    )

    m2m = await _resolve_m2m_fields(
        payload.cargo_keys,
        payload.source_area_names,
        payload.destination_area_names,
        payload.source_delivery_point_guids,
        payload.destination_delivery_point_guids,
    )
    for field_name, objects in m2m.items():
        await getattr(rule, field_name).aset(objects)

    await rule.arefresh_from_db()
    return 201, await _serialize_rule(rule)


@router.patch(
    "/subsidy-rules/{rule_id}/",
    response={200: SubsidyRuleDetailSchema, 404: ErrorSchema, 400: ErrorSchema},
)
async def update_subsidy_rule(
    request: HttpRequest, rule_id: int, payload: SubsidyRuleUpdateRequest
):
    try:
        rule = await SubsidyRule.objects.aget(pk=rule_id)
    except SubsidyRule.DoesNotExist:
        return 404, {"error": f"SubsidyRule {rule_id} not found"}

    if payload.reward_type is not None and payload.reward_type not in (
        "PERCENTAGE",
        "FLAT",
    ):
        return 400, {"error": "reward_type must be 'PERCENTAGE' or 'FLAT'"}
    if payload.reward_value is not None and payload.reward_value <= 0:
        return 400, {"error": "reward_value must be positive"}

    scalar_fields = [
        "name",
        "active",
        "priority",
        "reward_type",
        "reward_value",
        "scales_with_damage",
        "requires_on_time",
        "allocation",
    ]
    for field in scalar_fields:
        value = getattr(payload, field)
        if value is not None:
            setattr(rule, field, value)
    await rule.asave()

    m2m = await _resolve_m2m_fields(
        payload.cargo_keys,
        payload.source_area_names,
        payload.destination_area_names,
        payload.source_delivery_point_guids,
        payload.destination_delivery_point_guids,
    )
    for field_name, objects in m2m.items():
        await getattr(rule, field_name).aset(objects)

    await rule.arefresh_from_db()
    return 200, await _serialize_rule(rule)


@router.post(
    "/subsidy-rules/{rule_id}/deactivate/",
    response={200: SubsidyRuleDetailSchema, 404: ErrorSchema},
)
async def deactivate_subsidy_rule(request: HttpRequest, rule_id: int):
    try:
        rule = await SubsidyRule.objects.aget(pk=rule_id)
    except SubsidyRule.DoesNotExist:
        return 404, {"error": f"SubsidyRule {rule_id} not found"}

    rule.active = False
    await rule.asave()

    await rule.arefresh_from_db()
    return 200, await _serialize_rule(rule)


@router.post(
    "/subsidy-rules/reorder/",
    response={200: SuccessSchema, 400: ErrorSchema},
)
async def reorder_subsidy_rules(
    request: HttpRequest, payload: SubsidyRuleReorderRequest
):
    rules = {
        rule.id: rule
        async for rule in SubsidyRule.objects.filter(
            id__in=payload.ordered_ids
        )
    }
    if len(rules) != len(payload.ordered_ids):
        missing = set(payload.ordered_ids) - set(rules.keys())
        return 400, {"error": f"Rule IDs not found: {missing}"}

    for priority, rule_id in enumerate(payload.ordered_ids):
        rule = rules[rule_id]
        rule.priority = len(payload.ordered_ids) - priority
        await rule.asave()

    return 200, {"success": True, "message": f"Reordered {len(payload.ordered_ids)} rules"}


# ── JobPostingConfig endpoints ─────────────────────────────────────────


@router.get(
    "/job-config/",
    response=JobConfigSchema,
)
async def get_job_config(request: HttpRequest):
    config = await JobPostingConfig.aget_config()
    return {
        "target_success_rate": config.target_success_rate,
        "min_multiplier": config.min_multiplier,
        "max_multiplier": config.max_multiplier,
        "players_per_job": config.players_per_job,
        "min_base_jobs": config.min_base_jobs,
        "min_active_jobs": config.min_active_jobs,
        "posting_rate_multiplier": config.posting_rate_multiplier,
        "treasury_equilibrium": config.treasury_equilibrium,
        "treasury_sensitivity": config.treasury_sensitivity,
        "treasury_cap_ratio": config.treasury_cap_ratio,
        "max_posts_per_tick": config.max_posts_per_tick,
    }


@router.patch(
    "/job-config/",
    response={200: JobConfigSchema, 400: ErrorSchema},
)
async def update_job_config(
    request: HttpRequest, payload: JobConfigUpdateRequest
):
    config = await JobPostingConfig.aget_config()

    fields = [
        "target_success_rate",
        "min_multiplier",
        "max_multiplier",
        "players_per_job",
        "min_base_jobs",
        "min_active_jobs",
        "posting_rate_multiplier",
        "treasury_equilibrium",
        "treasury_sensitivity",
        "treasury_cap_ratio",
        "max_posts_per_tick",
    ]
    for field in fields:
        value = getattr(payload, field)
        if value is not None:
            setattr(config, field, value)
    await config.asave()

    return 200, {
        "target_success_rate": config.target_success_rate,
        "min_multiplier": config.min_multiplier,
        "max_multiplier": config.max_multiplier,
        "players_per_job": config.players_per_job,
        "min_base_jobs": config.min_base_jobs,
        "min_active_jobs": config.min_active_jobs,
        "posting_rate_multiplier": config.posting_rate_multiplier,
        "treasury_equilibrium": config.treasury_equilibrium,
        "treasury_sensitivity": config.treasury_sensitivity,
        "treasury_cap_ratio": config.treasury_cap_ratio,
        "max_posts_per_tick": config.max_posts_per_tick,
    }
