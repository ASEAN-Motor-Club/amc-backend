from ninja import Schema, ModelSchema
from ..models import Player, Character

class PlayerSchema(ModelSchema):
  unique_id: int

  class Meta:
    model = Player
    fields = ['unique_id', 'discord_user_id']


class CharacterSchema(ModelSchema):
  player_id: int

  class Meta:
    model = Character
    fields = ['id', 'name']


class PositionSchema(Schema):
  x: float
  y: float
  z: float
