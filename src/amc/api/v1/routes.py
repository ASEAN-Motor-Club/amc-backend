from datetime import timedelta
from asgiref.sync import sync_to_async
from django.db.models import Sum
from django.utils import timezone
from ninja import Router

from amc.api.v1.schema import (
    EconomyOverviewSchema,
    NPLLoanSchema,
    DonationsLeaderboardSchema,
    StorageItemSchema,
    DeliveryPointStorageSchema,
    CharacterProfileSchema,
    CharacterVehicleSchema,
    CharacterDeliverySchema,
    CharacterSessionSchema,
    VehicleCatalogSchema,
    SupplyChainEventSchema,
    SupplyChainEventListSchema,
    SupplyChainContributorSchema,
    ServerStatusSchema,
    PoliceStatsSchema,
    RescueRequestSchema,
    TeleportPointSchema,
)
from amc.models import (
    Character,
    CharacterVehicle,
    Delivery,
    DeliveryPoint,
    DeliveryPointStorage,
    PlayerStatusLog,
    ServerStatus,
    SupplyChainEvent,
    SupplyChainContribution,
    RescueRequest,
    TeleportPoint,
)
from amc.enums import VehicleKey, CargoKey, VEHICLE_DATA

# ═══════════════════════════════════════════════════════════════
# Phase 4: Economy & Real-Time Data
# ═══════════════════════════════════════════════════════════════

economy_router = Router()


@economy_router.get("/overview/", response=EconomyOverviewSchema)
async def economy_overview(request):
    """Aggregate economy statistics: treasury, donations, subsidies, loans."""
    from amc_finance.services import get_treasury_fund_balance
    from amc_finance.loans import get_non_performing_loans
    from amc_finance.models import Account, LedgerEntry

    treasury_balance = await get_treasury_fund_balance()

    total_donations = await LedgerEntry.objects.filter_donations().aaggregate(
        total=Sum("credit", default=0)
    )
    total_subsidies = await LedgerEntry.objects.filter_subsidies().aaggregate(
        total=Sum("debit", default=0)
    )
    active_loan_count = await Account.objects.filter(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.BANK,
        character__isnull=False,
        balance__gt=0,
    ).acount()

    npl_loans = await sync_to_async(get_non_performing_loans)()

    return {
        "treasury_balance": float(treasury_balance),
        "total_donations_all_time": float(total_donations["total"]),
        "total_subsidy_spend_all_time": float(total_subsidies["total"]),
        "active_loan_count": active_loan_count,
        "npl_count": len(npl_loans),
    }


@economy_router.get("/npl/", response=list[NPLLoanSchema])
async def npl_loans(request):
    """List characters with non-performing loans (public transparency)."""
    from amc_finance.loans import get_non_performing_loans

    loans = await sync_to_async(get_non_performing_loans)()

    return [
        {
            "character_id": account.character.id,
            "character_name": account.character.name,
            "loan_balance": float(account.balance),
            "total_repaid_in_period": float(account.total_repaid_in_period),
            "min_required_repayment": float(account.min_required_repayment),
            "repayment_period_days": account.repayment_period_days,
        }
        for account in loans
    ]


@economy_router.get(
    "/donations/leaderboard/", response=list[DonationsLeaderboardSchema]
)
async def donations_leaderboard(request, limit: int = 20):
    """Top donors by total lifetime donations."""
    characters = Character.objects.filter(total_donations__gt=0).order_by(
        "-total_donations"
    )[:limit]

    return [
        {
            "character_id": character.id,
            "character_name": character.name,
            "total_donations": float(character.total_donations),
        }
        async for character in characters
    ]


# ── Delivery Point Storage ───────────────────────────────────────────

storage_router = Router()


@storage_router.get("/{guid}/storage/", response=list[StorageItemSchema])
async def delivery_point_storage(request, guid: str):
    """Current storage levels at a single delivery point."""
    storages = DeliveryPointStorage.objects.filter(
        delivery_point__guid=guid
    ).select_related("cargo")

    return [
        {
            "cargo_key": s.cargo_key,
            "cargo_label": s.cargo.label if s.cargo else None,
            "kind": s.kind,
            "amount": s.amount,
            "capacity": s.capacity,
        }
        async for s in storages
    ]


@storage_router.get("/storage/", response=list[DeliveryPointStorageSchema])
async def all_storage(request):
    """Bulk snapshot of storage levels across all delivery points."""
    delivery_points = DeliveryPoint.objects.prefetch_related(
        "storages", "storages__cargo"
    ).all()

    results = []
    async for dp in delivery_points:
        storages = [
            {
                "cargo_key": s.cargo_key,
                "cargo_label": s.cargo.label if s.cargo else None,
                "kind": s.kind,
                "amount": s.amount,
                "capacity": s.capacity,
            }
            async for s in dp.storages.all()
        ]
        if storages:
            results.append(
                {
                    "delivery_point_guid": str(dp.guid),
                    "delivery_point_name": dp.name or "",
                    "storages": storages,
                }
            )

    return results


# ── Character Profiles & Data ────────────────────────────────────────

characters_router = Router()


@characters_router.get("/{int:character_id}/profile/", response=CharacterProfileSchema)
async def character_profile(request, character_id: int):
    """Extended character profile (privacy-safe: no money, no social score)."""
    from amc_finance.loans import get_credit_score_label

    character = await Character.objects.select_related("player").aget(id=character_id)

    is_gov_employee = (
        character.gov_employee_until is not None
        and character.gov_employee_until > timezone.now()
    )

    return {
        "id": character.id,
        "name": character.name,
        "player_id": str(character.player.unique_id),
        "driver_level": character.driver_level,
        "bus_level": character.bus_level,
        "taxi_level": character.taxi_level,
        "police_level": character.police_level,
        "truck_level": character.truck_level,
        "wrecker_level": character.wrecker_level,
        "racer_level": character.racer_level,
        "credit_score_tier": get_credit_score_label(character.credit_score),
        "is_government_employee": is_gov_employee,
        "total_donations": float(character.total_donations),
    }


@characters_router.get(
    "/{int:character_id}/vehicles/", response=list[CharacterVehicleSchema]
)
async def character_vehicles(request, character_id: int):
    """Vehicles owned by a character."""
    vehicles = CharacterVehicle.objects.filter(character_id=character_id)

    return [
        {
            "id": v.id,
            "vehicle_id": v.vehicle_id,
            "vehicle_name": v.config.get("VehicleName") if v.config else None,
            "alias": v.alias,
            "for_sale": v.for_sale,
            "rental": v.rental,
        }
        async for v in vehicles
    ]


@characters_router.get(
    "/{int:character_id}/deliveries/", response=list[CharacterDeliverySchema]
)
async def character_deliveries(
    request, character_id: int, limit: int = 50, offset: int = 0
):
    """Delivery history for a character (no payment amounts — privacy)."""
    deliveries = Delivery.objects.filter(character_id=character_id).order_by(
        "-timestamp"
    )[offset : offset + limit]

    return [
        {
            "id": d.id,
            "timestamp": d.timestamp.isoformat(),
            "cargo_key": d.cargo_key,
            "quantity": d.quantity,
            "rp_mode": d.rp_mode,
        }
        async for d in deliveries
    ]


@characters_router.get(
    "/{int:character_id}/sessions/", response=list[CharacterSessionSchema]
)
async def character_sessions(
    request, character_id: int, days: int = 30, limit: int = 50
):
    """Login/logout session history for a character (last N days)."""
    cutoff = timezone.now() - timedelta(days=days)
    sessions = PlayerStatusLog.objects.filter(
        character_id=character_id,
        timespan__startswith__gte=cutoff,
    ).order_by("-timespan")[:limit]

    return [
        {
            "start_time": s.timespan.lower.isoformat() if s.timespan.lower else "",
            "end_time": s.timespan.upper.isoformat() if s.timespan.upper else None,
            "duration_seconds": int(
                (s.timespan.upper - s.timespan.lower).total_seconds()
            )
            if s.timespan.lower and s.timespan.upper
            else 0,
        }
        async for s in sessions
    ]


# ── Vehicle Catalog & Enums ──────────────────────────────────────────

vehicles_router = Router()


@vehicles_router.get("/catalog/", response=list[VehicleCatalogSchema])
async def vehicle_catalog(request):
    """Full vehicle catalog with costs from game data."""
    return [
        {
            "key": key,
            "label": VehicleKey(key).label if key in VehicleKey.values else key,
            "cost": data["cost"],
        }
        for key, data in VEHICLE_DATA.items()
    ]


@vehicles_router.get("/enums/", response=dict)
async def vehicle_enums(request):
    """Vehicle and cargo key enumerations."""
    return {
        "vehicles": [
            {"value": value, "label": label} for value, label in VehicleKey.choices
        ],
        "cargos": [
            {"value": value, "label": label} for value, label in CargoKey.choices
        ],
    }


# ═══════════════════════════════════════════════════════════════
# Phase 5: Events & Competition
# ═══════════════════════════════════════════════════════════════

supply_chain_router = Router()


@supply_chain_router.get("/", response=list[SupplyChainEventListSchema])
async def list_supply_chain_events(request):
    """List active and upcoming supply chain events."""
    events = SupplyChainEvent.objects.filter_active_or_future().order_by("start_at")

    return [
        {
            "id": event.id,
            "name": event.name,
            "start_at": event.start_at.isoformat(),
            "end_at": event.end_at.isoformat(),
            "is_active": event.is_active,
        }
        async for event in events
    ]


@supply_chain_router.get("/{int:event_id}/", response=SupplyChainEventSchema)
async def supply_chain_event_detail(request, event_id: int):
    """Supply chain event with objectives and progress."""
    event = await SupplyChainEvent.objects.aget(id=event_id)

    objectives = []
    async for obj in event.objectives.prefetch_related("cargos").all():
        cargo_names = [c.label async for c in obj.cargos.all()]
        objectives.append(
            {
                "id": obj.id,
                "cargo_names": cargo_names,
                "quantity_fulfilled": obj.quantity_fulfilled,
                "ceiling": obj.ceiling,
                "reward_weight": obj.reward_weight,
                "is_primary": obj.is_primary,
            }
        )

    return {
        "id": event.id,
        "name": event.name,
        "description": event.description,
        "start_at": event.start_at.isoformat(),
        "end_at": event.end_at.isoformat(),
        "reward_per_item": event.reward_per_item,
        "is_active": event.is_active,
        "objectives": objectives,
    }


@supply_chain_router.get(
    "/{int:event_id}/leaderboard/", response=list[SupplyChainContributorSchema]
)
async def supply_chain_leaderboard(request, event_id: int, limit: int = 20):
    """Top contributors to a supply chain event."""
    contributors = (
        SupplyChainContribution.objects.filter(objective__event_id=event_id)
        .values("character_id", "character__name")
        .annotate(total_quantity=Sum("quantity"))
        .order_by("-total_quantity")[:limit]
    )

    return [
        {
            "character_id": c["character_id"],
            "character_name": c["character__name"],
            "total_quantity": c["total_quantity"],
        }
        async for c in contributors
    ]


# ═══════════════════════════════════════════════════════════════
# Phase 6: Server & Community
# ═══════════════════════════════════════════════════════════════

server_router = Router()


@server_router.get("/status/", response={200: ServerStatusSchema, 204: None})
async def server_status(request):
    """Current server status (latest sample)."""
    status = await ServerStatus.objects.order_by("-timestamp").afirst()
    if not status:
        return 204, None

    return {
        "timestamp": status.timestamp.isoformat(),
        "num_players": status.num_players,
        "fps": status.fps,
        "used_memory": status.used_memory,
        "fd_total": status.fd_total,
        "fd_max_num": status.fd_max_num,
    }


@server_router.get("/status/history/", response=list[ServerStatusSchema])
async def server_status_history(request, hours: int = 24, limit: int = 288):
    """Server status history (default last 24h, sampled at ~5min intervals = 288)."""
    cutoff = timezone.now() - timedelta(hours=hours)
    statuses = ServerStatus.objects.filter(timestamp__gte=cutoff).order_by(
        "-timestamp"
    )[:limit]

    return [
        {
            "timestamp": s.timestamp.isoformat(),
            "num_players": s.num_players,
            "fps": s.fps,
            "used_memory": s.used_memory,
            "fd_total": s.fd_total,
            "fd_max_num": s.fd_max_num,
        }
        async for s in statuses
    ]


police_router = Router()


@police_router.get("/stats/", response=PoliceStatsSchema)
async def police_stats(request, days: int = 7):
    """Aggregate police activity (anonymized — no player names)."""
    from amc.models import PolicePatrolLog, PolicePenaltyLog, PoliceShiftLog

    cutoff = timezone.now() - timedelta(days=days)

    total_patrols = await PolicePatrolLog.objects.filter(timestamp__gte=cutoff).acount()

    total_penalties = await PolicePenaltyLog.objects.filter(
        timestamp__gte=cutoff
    ).acount()

    # Active shifts: count START actions minus END actions in period
    starts = await PoliceShiftLog.objects.filter(
        timestamp__gte=cutoff, action=PoliceShiftLog.Action.START
    ).acount()
    ends = await PoliceShiftLog.objects.filter(
        timestamp__gte=cutoff, action=PoliceShiftLog.Action.END
    ).acount()
    active_shifts = max(0, starts - ends)

    return {
        "total_patrols": total_patrols,
        "total_penalties": total_penalties,
        "active_shifts": active_shifts,
    }


rescue_router = Router()


@rescue_router.get("/recent/", response=list[RescueRequestSchema])
async def recent_rescues(request, limit: int = 20):
    """Recent rescue requests (anonymized — no player names)."""
    rescues = RescueRequest.objects.order_by("-timestamp")[:limit]

    return [
        {
            "id": r.id,
            "timestamp": r.timestamp.isoformat(),
            "responder_count": await r.responders.acount(),
            "message": r.message or "",
            "location": (
                {"x": r.location.x, "y": r.location.y, "z": r.location.z}
                if r.location
                else None
            ),
        }
        async for r in rescues
    ]


teleport_router = Router()


@teleport_router.get("/", response=list[TeleportPointSchema])
async def list_public_teleport_points(request):
    teleport_points = TeleportPoint.objects.filter(character__isnull=True)

    return [
        {
            "name": tp.name,
            "x": tp.location.x,
            "y": tp.location.y,
            "z": tp.location.z,
        }
        async for tp in teleport_points
    ]
