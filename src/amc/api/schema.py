from typing import Optional
from pydantic import AwareDatetime
from datetime import timedelta
from ninja import Schema, ModelSchema
from ..models import Player, Character

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
