from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from django.contrib.gis.geos import Point
from asgiref.sync import sync_to_async
from amc.factories import CharacterFactory, ChampionshipFactory, ChampionshipPointFactory
from amc.models import CharacterLocation, Character

class CharacterLocationTestCase(TestCase):
  async def test_activity(self):
    character = await sync_to_async(CharacterFactory)()
    for n in range(0, 1000): 
      await CharacterLocation.objects.acreate(
        timestamp=timezone.now() - timedelta(hours=1) + timedelta(seconds=n),
        character=character,
        location=Point(10000 + n * 100, 10000 + n * 100, 0),
      )
    is_online, is_active = await CharacterLocation.get_character_activity(
      character,
      timezone.now() - timedelta(hours=1),
      timezone.now() - timedelta(seconds=0),
    )
    self.assertTrue(is_online)
    self.assertTrue(is_active)

  async def test_activity_offline(self):
    character = await sync_to_async(CharacterFactory)()
    is_online, is_active = await CharacterLocation.get_character_activity(
      character,
      timezone.now() - timedelta(hours=1),
      timezone.now() - timedelta(seconds=0),
    )
    self.assertFalse(is_online)
    self.assertFalse(is_active)

  async def test_activity_afk(self):
    character = await sync_to_async(CharacterFactory)()
    for n in range(0, 1000): 
      await CharacterLocation.objects.acreate(
        timestamp=timezone.now() - timedelta(hours=1) + timedelta(seconds=n),
        character=character,
        location=Point(10000 + n * 0.1, 10000 + n * 0.1, 0),
      )
    is_online, is_active = await CharacterLocation.get_character_activity(
      character,
      timezone.now() - timedelta(hours=1),
      timezone.now() - timedelta(seconds=0),
    )
    self.assertTrue(is_online)
    self.assertFalse(is_active)

class ChampionshipTestCase(TestCase):
  async def test_award_personal_prizes(self):
    championship = await sync_to_async(ChampionshipFactory)()
    await sync_to_async(ChampionshipPointFactory)(
      championship=championship,
    )
    await sync_to_async(ChampionshipPointFactory)(
      championship=championship,
    )
    prizes = await championship.calculate_personal_prizes()
    print(prizes)

  async def test_award_team_prizes(self):
    championship = await sync_to_async(ChampionshipFactory)()
    p1 = await sync_to_async(ChampionshipPointFactory)(
      championship=championship,
    )
    await sync_to_async(ChampionshipPointFactory)(
      championship=championship,
      team=p1.team
    )
    await sync_to_async(ChampionshipPointFactory)(
      championship=championship,
    )
    prizes = await championship.calculate_team_prizes()
    print(prizes)

class CharacterMangerTestCase(TestCase):
  async def test_change_name(self):
    character1, *_ = await Character.objects.aget_or_create_character_player('test', 123, character_guid=234)
    character2, *_ = await Character.objects.aget_or_create_character_player('test2', 123, character_guid=234)
    self.assertEqual(character1.id, character2.id)

  async def test_add_guid(self):
    character1, *_ = await Character.objects.aget_or_create_character_player('test', 123)
    character2, *_ = await Character.objects.aget_or_create_character_player('test', 123, character_guid=234)
    self.assertEqual(character1.id, character2.id)

  async def test_missing_guid(self):
    character1, *_ = await Character.objects.aget_or_create_character_player('test', 123, character_guid=234)
    character2, *_ = await Character.objects.aget_or_create_character_player('test', 123)
    self.assertEqual(character1.id, character2.id)

  async def test_new_alt(self):
    character1, *_ = await Character.objects.aget_or_create_character_player('test', 123, character_guid=234)
    character2, *_ = await Character.objects.aget_or_create_character_player('test', 123, character_guid=345)
    self.assertNotEqual(character1.id, character2.id)

