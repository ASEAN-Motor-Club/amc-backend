from typing import Optional
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
  DeliveryPoint,
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
  player_id: str = Field(None, alias="character.player.unqiue_id")
  character_name: str = Field(None, alias="character.name")

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
  team_id: Optional[int]
  team_name: Optional[str]

  class Config(Schema.Config):
    coerce_numbers_to_str = True


class TeamStandingSchema(Schema):
  total_points: int
  team_id: int = Field(None, alias="team__id")
  team_tag: str = Field(None, alias="team__tag")
  team_name: str = Field(None, alias="team__name")


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
