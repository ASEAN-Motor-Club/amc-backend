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
  Character,
  Team,
  ScheduledEvent,
  GameEvent,
  GameEventCharacter,
  Championship,
  ChampionshipPoint,
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

class TeamFactory(DjangoModelFactory):
  class Meta:
    model = Team

  name = Faker('company')
  tag = Faker('country_code')
  description = Faker('catch_phrase')
  discord_thread_id = LazyAttribute(lambda _: str(random.randint(100000000000000000, 999999999999999999)))

class ScheduledEventFactory(DjangoModelFactory):
  class Meta:
    model = ScheduledEvent

  name = Faker('company')
  description = Faker('catch_phrase')
  discord_event_id = LazyAttribute(lambda _: str(random.randint(100000000000000000, 999999999999999999)))
  discord_thread_id = LazyAttribute(lambda _: str(random.randint(100000000000000000, 999999999999999999)))
  start_time = Faker('date_time')

class GameEventFactory(DjangoModelFactory):
  class Meta:
    model = GameEvent

  name = Faker('company')
  guid = Faker('company')
  state = LazyAttribute(lambda _: random.randint(1, 3))
  discord_message_id = LazyAttribute(lambda _: str(random.randint(100000000000000000, 999999999999999999)))
  scheduled_event = SubFactory('amc.factories.ScheduledEventFactory')

class ChampionshipFactory(DjangoModelFactory):
  class Meta:
    model = Championship

  name = Faker('company')
  discord_thread_id = LazyAttribute(lambda _: str(random.randint(100000000000000000, 999999999999999999)))
  description = Faker('catch_phrase')

class ChampionshipPointFactory(DjangoModelFactory):
  class Meta:
    model = ChampionshipPoint

  championship = SubFactory('amc.factories.ChampionshipFactory')
  participant = SubFactory('amc.factories.GameEventCharacterFactory')
  team = SubFactory('amc.factories.TeamFactory')
  points = LazyAttribute(lambda _: str(random.randint(0, 25)))

class GameEventCharacterFactory(DjangoModelFactory):
  class Meta:
    model = GameEventCharacter

  character = SubFactory('amc.factories.CharacterFactory')
  game_event = SubFactory('amc.factories.GameEventFactory')
  rank = LazyAttribute(lambda _: random.randint(1, 20))
  best_lap_time = LazyAttribute(lambda _: random.randint(100, 1000))
  finished = True
  last_section_total_time_seconds = LazyAttribute(lambda p: random.randint(100, 1000) if p.finished else None)
  first_section_total_time_seconds = LazyAttribute(lambda p: random.randint(0, 99) if p.finished else None)

