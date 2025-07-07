from ninja import Schema, ModelSchema
from ..models import Player

class PlayerSchema(ModelSchema):
  unique_id: int

  class Meta:
    model = Player
    fields = ['unique_id', 'discord_user_id']

class PositionSchema(Schema):
  x: float
  y: float
  z: float
