import random
from factory import (
  SubFactory,
  Faker,
  LazyAttribute,
  RelatedFactoryList,
)
from factory.django import DjangoModelFactory
from .models import (
  Player,
  Character
)

class CharacterFactory(DjangoModelFactory):
  class Meta:
    model = Character

  player = SubFactory('amc.factories.PlayerFactory', characters=None)
  name = Faker('user_name')

class PlayerFactory(DjangoModelFactory):
  class Meta:
    model = Player

  characters = RelatedFactoryList(
    CharacterFactory,
    size=lambda: random.randint(1, 5),
    factory_related_name='player'
  )
  unique_id = LazyAttribute(lambda _: random.randint(10000000000000000, 99999999999999999))
  discord_user_id = LazyAttribute(lambda _: random.randint(100000000000000000, 999999999999999999))

