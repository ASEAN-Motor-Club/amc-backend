from typing import Optional
from ninja import Schema


# ── Economy Schemas ──────────────────────────────────────────────────


class EconomyOverviewSchema(Schema):
    treasury_balance: float
    total_donations_all_time: float
    total_subsidy_spend_all_time: float
    active_loan_count: int
    npl_count: int


class NPLLoanSchema(Schema):
    character_id: int
    character_name: str
    loan_balance: float
    total_repaid_in_period: float
    min_required_repayment: float
    repayment_period_days: int


class DonationsLeaderboardSchema(Schema):
    character_id: int
    character_name: str
    total_donations: float


# ── Delivery Point Storage Schemas ───────────────────────────────────


class StorageItemSchema(Schema):
    cargo_key: str
    cargo_label: Optional[str] = None
    kind: str  # "IN" or "OU"
    amount: int
    capacity: Optional[int] = None


class DeliveryPointStorageSchema(Schema):
    delivery_point_guid: str
    delivery_point_name: str
    storages: list[StorageItemSchema]


# ── Character Schemas ────────────────────────────────────────────────


class CharacterProfileSchema(Schema):
    id: int
    name: str
    player_id: str
    driver_level: Optional[int] = None
    bus_level: Optional[int] = None
    taxi_level: Optional[int] = None
    police_level: Optional[int] = None
    truck_level: Optional[int] = None
    wrecker_level: Optional[int] = None
    racer_level: Optional[int] = None
    credit_score_tier: str
    is_government_employee: bool
    total_donations: float


class CharacterVehicleSchema(Schema):
    id: int
    vehicle_id: int
    vehicle_name: Optional[str] = None
    alias: Optional[str] = None
    for_sale: bool
    rental: bool


class CharacterDeliverySchema(Schema):
    id: int
    timestamp: str
    cargo_key: str
    quantity: int
    rp_mode: bool


class CharacterSessionSchema(Schema):
    start_time: str
    end_time: Optional[str] = None
    duration_seconds: int


# ── Vehicle Schemas ──────────────────────────────────────────────────


class VehicleCatalogSchema(Schema):
    key: str
    label: str
    cost: int


class EnumValueSchema(Schema):
    value: str
    label: str


# ── Supply Chain Schemas ─────────────────────────────────────────────


class SupplyChainObjectiveSchema(Schema):
    id: int
    cargo_names: list[str]
    quantity_fulfilled: int
    ceiling: Optional[int] = None
    reward_weight: int
    is_primary: bool


class SupplyChainEventSchema(Schema):
    id: int
    name: str
    description: str
    start_at: str
    end_at: str
    reward_per_item: int
    is_active: bool
    objectives: list[SupplyChainObjectiveSchema]


class SupplyChainEventListSchema(Schema):
    id: int
    name: str
    start_at: str
    end_at: str
    is_active: bool


class SupplyChainContributorSchema(Schema):
    character_id: int
    character_name: str
    total_quantity: int


# ── Server Status Schemas ────────────────────────────────────────────


class ServerStatusSchema(Schema):
    timestamp: str
    num_players: int
    fps: int
    used_memory: int
    fd_total: Optional[int] = None
    fd_max_num: Optional[int] = None


# ── Police / Rescue Schemas ──────────────────────────────────────────


class PoliceStatsSchema(Schema):
    total_patrols: int
    total_penalties: int
    active_shifts: int


class RescueRequestSchema(Schema):
    id: int
    timestamp: str
    responder_count: int
    message: str
    location: Optional[dict] = None


class TeleportPointSchema(Schema):
    name: str
    x: float
    y: float
    z: float
