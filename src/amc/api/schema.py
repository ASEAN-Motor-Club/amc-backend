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
)


class ActivePlayerSchema(Schema):
  name: str
  unique_id: str


class PlayerSchema(ModelSchema):
  unique_id: str
  total_session_time: timedelta
  last_login: Optional[AwareDatetime]

  class Meta:
    model = Player
    fields = ['unique_id', 'discord_user_id']

  class Config(Schema.Config):
    coerce_numbers_to_str = True


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
    ]

