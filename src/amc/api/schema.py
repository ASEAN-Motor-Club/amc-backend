from datetime import timedelta
from ninja import Schema, ModelSchema
from ..models import Player, Character

class PlayerSchema(ModelSchema):
  unique_id: int
  total_session_time: timedelta

  class Meta:
    model = Player
    fields = ['unique_id', 'discord_user_id']


class CharacterSchema(ModelSchema):
  player_id: int

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


class PositionSchema(Schema):
  x: float
  y: float
  z: float
