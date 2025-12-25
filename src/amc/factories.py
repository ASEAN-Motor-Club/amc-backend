import random
import uuid
from django.utils import timezone
from django.contrib.gis.geos import Point
from datetime import timedelta
import factory
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
  DeliveryPoint,
  Cargo,
  DeliveryJobTemplate,
  DeliveryJob,
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
  end_time = LazyAttribute(lambda e: e.start_time + timedelta(days=3))

class GameEventFactory(DjangoModelFactory):
  class Meta:
    model = GameEvent

  name = Faker('company')
  guid = Faker('company')
  state = LazyAttribute(lambda _: random.randint(1, 3))
  discord_message_id = LazyAttribute(lambda _: str(random.randint(100000000000000000, 999999999999999999)))
  scheduled_event = SubFactory('amc.factories.ScheduledEventFactory')
  start_time = LazyAttribute(lambda e: e.scheduled_event.start_time + timedelta(hours=1))

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

class CargoFactory(DjangoModelFactory):
  class Meta:
    model = Cargo

class DeliveryPointFactory(DjangoModelFactory):
  class Meta:
    model = DeliveryPoint

  guid = LazyAttribute(lambda _: uuid.uuid4())
  coord = LazyAttribute(lambda _: Point(random.randint(100, 1000), random.randint(100, 1000), random.randint(100, 1000)))

class DeliveryJobFactory(DjangoModelFactory):
  class Meta:
    model = DeliveryJob

  quantity_requested = LazyAttribute(lambda _: random.randint(1, 1000))
  bonus_multiplier = LazyAttribute(lambda _: random.random())
  expired_at = LazyAttribute(lambda _: timezone.now() + timedelta(hours=random.randint(2, 8)))

class DeliveryJobTemplateFactory(DjangoModelFactory):
  class Meta:
    model = DeliveryJobTemplate

  name = Faker('bs')
  default_quantity = LazyAttribute(lambda _: random.randint(1, 1000))
  bonus_multiplier = LazyAttribute(lambda _: random.random())
  duration_hours = LazyAttribute(lambda _: random.randint(2, 8))

  @factory.post_generation
  def cargos(self, create, extracted, **kwargs):
    if not create:
        return
    if extracted:
        self.cargos.add(*extracted)

  @factory.post_generation
  def source_points(self, create, extracted, **kwargs):
    if not create:
        return
    if extracted:
        self.source_points.add(*extracted)

  @factory.post_generation
  def destination_points(self, create, extracted, **kwargs):
    if not create:
        return
    if extracted:
        self.destination_points.add(*extracted)

