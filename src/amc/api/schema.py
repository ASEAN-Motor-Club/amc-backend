from typing import Optional, List, TypeVar, Generic
from pydantic import AwareDatetime
from datetime import timedelta
from ninja import Schema, ModelSchema, Field
from ..models import (
  Player,
  Character,
  CharacterLocation,
  Team,
  ScheduledEvent,
  GameEventCharacter,
  ChampionshipPoint,
  Championship,
  DeliveryPoint,
  Cargo,
  Delivery,
  DeliveryJob,
  LapSectionTime,
)


class ActivePlayerSchema(Schema):
  name: str
  unique_id: str


class CharacterSchema(ModelSchema):
  player_id: str

  class Meta:
    model = Character
    fields = [
      'id',
      'name',
      'driver_level',
      'bus_level',
      'taxi_level',
      'police_level',
      'truck_level',
      'wrecker_level',
      'racer_level',
    ]

  class Config(Schema.Config):
    coerce_numbers_to_str = True


class PlayerSchema(ModelSchema):
  unique_id: str
  total_session_time: timedelta
  last_login: Optional[AwareDatetime] = None
  main_character: Optional[CharacterSchema] = None

  class Meta:
    model = Player
    fields = ['unique_id', 'discord_user_id']

  class Config(Schema.Config):
    coerce_numbers_to_str = True

  @staticmethod
  def resolve_main_character(obj):
    if not obj.main_characters:
      return None
    return obj.main_characters[0]


class PositionSchema(Schema):
  x: float
  y: float
  z: float


class LeaderboardsRestockDepotCharacterSchema(CharacterSchema):
  depots_restocked: int


class CharacterLocationSchema(ModelSchema):
  location: PositionSchema
  player_id: Optional[str] = Field(None, alias="character.player.unqiue_id")
  character_name: Optional[str] = Field(None, alias="character.name")

  class Meta:
    model = CharacterLocation
    fields = ['timestamp', 'character']


class TeamSchema(ModelSchema):
  players: list[PlayerSchema]

  class Meta:
    model = Team
    fields = [
      'id',
      'name',
      'tag',
      'description',
      'logo',
      'bg_color',
      'text_color',
    ]

class SimpleTeamSchema(ModelSchema):
  class Meta:
    model = Team
    fields = [
      'id',
      'name',
      'tag',
      'description',
      'logo',
      'bg_color',
      'text_color',
    ]

class PatchTeamSchema(ModelSchema):
  class Meta:
    model = Team
    fields = [
      'description',
      'bg_color',
      'text_color',
    ]
    fields_optional = '__all__'


class ScheduledEventSchema(ModelSchema):
  class Meta:
    model = ScheduledEvent
    fields = [
      'id',
      'name',
      'start_time',
      'end_time',
      'discord_event_id',
      'race_setup',
      'description',
      'time_trial',
    ]

class ChampionshipPointSchema(ModelSchema):
  team: Optional[SimpleTeamSchema] = None

  class Meta:
    model = ChampionshipPoint
    fields = [
      'team',
      'points',
    ]

class ParticipantSchema(ModelSchema):
  character: CharacterSchema
  net_time: Optional[float]
  championship_point: Optional[ChampionshipPointSchema] = None

  class Meta:
    model = GameEventCharacter
    fields = [
      'id',
      'finished',
      'net_time',
      'laps',
      'section_index',
      'first_section_total_time_seconds',
      'last_section_total_time_seconds',
    ]

class LapSectionTimeSchema(ModelSchema):
  net_time: Optional[float] = None
  section_duration: Optional[float] = None

  class Meta:
    model = LapSectionTime
    fields = [
      'id',
      'lap',
      'section_index',
      'total_time_seconds',
    ]

class PersonalStandingSchema(Schema):
  total_points: int
  player_id: str
  character_name: str

  class Config(Schema.Config):
    coerce_numbers_to_str = True


class TeamStandingSchema(Schema):
  total_points: int
  team_id: Optional[int] = Field(None, alias="team__id")
  team_tag: Optional[str] = Field(None, alias="team__tag")
  team_name: Optional[str] = Field(None, alias="team__name")


class DeliveryPointSchema(ModelSchema):
  coord: PositionSchema

  class Meta:
    model = DeliveryPoint
    fields = [
      'guid',
      'name',
      'type',
      'data',
      'last_updated',
    ]

class CargoSchema(ModelSchema):
  class Meta:
    model = Cargo
    fields = ['key', 'label']

class DeliverySchema(ModelSchema):
  character: CharacterSchema

  class Meta:
    model = Delivery
    fields = ['timestamp', 'character', 'cargo_key', 'quantity', 'payment', 'subsidy']

class DeliveryJobSchema(ModelSchema):
  cargos: List[CargoSchema]
  source_points: List[DeliveryPointSchema]
  destination_points: List[DeliveryPointSchema]
  deliveries: List[DeliverySchema]

  class Meta:
    model = DeliveryJob
    fields = [
      'id',
      'name',
      'quantity_requested',
      'quantity_fulfilled',
      'requested_at',
      'fulfilled_at',
      'expired_at',
      'bonus_multiplier',
      'completion_bonus',
      # 'discord_message_id',
      'description',
      # 'template',
      # 'base_template',
      # 'expected_player_count_for_quantity',
      # 'job_posting_probability',
      # 'template_job_period_hours',
      'fulfilled',
    ]

class WebhookPayloadSchema(Schema):
  data: dict
  hook: str
  timestamp: int


# Phase 1: Public API Schemas

class SubsidyRulePublicSchema(Schema):
  """Public-safe subsidy rule information"""
  id: int
  name: str
  active: bool
  priority: int
  reward_type: str
  reward_value: float
  cargo_keys: List[str] = []
  source_area_names: List[str] = []
  destination_area_names: List[str] = []
  requires_on_time: bool


class MinistryTermPublicSchema(Schema):
  """Public ministry term information"""
  id: int
  minister_name: str
  minister_id: str
  start_date: AwareDatetime
  end_date: AwareDatetime
  initial_budget: float
  current_budget: float
  total_spent: float
  is_active: bool
  created_jobs_count: int
  expired_jobs_count: int

  class Config(Schema.Config):
    coerce_numbers_to_str = True


class ChampionshipSchema(ModelSchema):
  """Championship basic information"""
  class Meta:
    model = Championship
    fields = ['id', 'name', 'description', 'discord_thread_id']


class DeliveryStatsSchema(Schema):
  """Delivery statistics by character"""
  character_id: int
  character_name: str
  player_id: str
  total_deliveries: int
  total_payment: int
  total_subsidy: int
  total_quantity: int

  class Config(Schema.Config):
    coerce_numbers_to_str = True


# Phase 2: Community Features Schemas

class CompanyPublicSchema(Schema):
  """Public company information"""
  id: int
  name: str
  description: str
  owner_name: str
  is_corp: bool
  first_seen_at: AwareDatetime


class MinistryCandidacySchema(Schema):
  """Ministry candidacy information"""
  candidate_name: str
  candidate_id: str
  manifesto: str
  created_at: AwareDatetime
  vote_count: int = 0

  class Config(Schema.Config):
    coerce_numbers_to_str = True


class MinistryElectionPublicSchema(Schema):
  """Public ministry election information"""
  id: int
  phase: str
  created_at: AwareDatetime
  candidacy_end_at: AwareDatetime
  poll_end_at: AwareDatetime
  winner_name: Optional[str] = None
  candidates: List[MinistryCandidacySchema] = []


class RaceSetupListSchema(Schema):
  """Race setup list information"""
  hash: str
  route_name: Optional[str] = None
  num_laps: int
  num_sections: int

# Phase 3: Extended Data Schemas

class SubsidyAreaSchema(Schema):
  """Subsidy area geographic information"""
  id: int
  name: str
  description: str
  # Note: polygon field would need special serialization


class PassengerStatsSchema(Schema):
  """Passenger transport statistics by character"""
  character_id: int
  character_name: str
  player_id: str
  total_passengers: int
  total_payment: int
  passenger_type_counts: dict

  class Config(Schema.Config):
    coerce_numbers_to_str = True


class VehicleDecalPublicSchema(Schema):
  """Public vehicle decal information"""
  id: int
  name: str
  vehicle_key: Optional[str] = None
  hash: str
  price: int
  player_name: Optional[str] = None


class VehicleDealershipSchema(Schema):
  """Vehicle dealership location"""
  id: int
  vehicle_key: Optional[str] = None
  location: PositionSchema
  notes: str

# Pagination Schema

T = TypeVar('T')

class PaginatedResponseSchema(Schema, Generic[T]):
  """Generic paginated response"""
  items: List[T]
  total: int
  offset: int
  limit: int
  has_more: bool

# Server Commands Schema

class ServerCommandSchema(Schema):
  """Server-side command information"""
  command: str
  aliases: List[str]
  shorthand: Optional[str] = None
  description: str
  category: str
  deprecated: bool = False
