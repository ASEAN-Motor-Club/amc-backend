from typing import cast, Any
from django.utils import timezone
from asgiref.sync import sync_to_async
from django.test import TestCase
from ninja.testing import TestAsyncClient
from amc.api.routes import (
  cargos_router,
  subsidies_rules_router,
  ministry_router,
  championships_list_router,
  deliveries_stats_router,
)
from amc.factories import (
  CargoFactory,
  SubsidyRuleFactory,
  MinistryTermFactory,
  DeliveryFactory,
  ChampionshipFactory,
  CharacterFactory,
)


class CargosAPITest(TestCase):
  """Test the /cargos/ endpoint"""
  def setUp(self):
    self.api_client = TestAsyncClient(cargos_router)

  async def test_list_cargos(self):
    """Test GET /cargos/ returns all cargo types"""
    await sync_to_async(CargoFactory)(key='C::Stone', label='Stone')
    await sync_to_async(CargoFactory)(key='C::Wood', label='Wood')
    
    response = await cast(Any, self.api_client.get("/"))
    
    self.assertEqual(response.status_code, 200)
    data = response.json()
    self.assertGreaterEqual(len(data), 2, "Should have at least 2 cargos")
    # Find our test cargos in the response
    cargo_keys = [c['key'] for c in data]
    self.assertIn('C::Stone', cargo_keys)
    self.assertIn('C::Wood', cargo_keys)


class SubsidyRulesAPITest(TestCase):
  """Test the /subsidies/rules/ endpoint"""
  def setUp(self):
    self.api_client = TestAsyncClient(subsidies_rules_router)

  async def test_list_active_rules(self):
    """Test GET /subsidies/rules/ returns only active rules"""
    await sync_to_async(SubsidyRuleFactory)(
      name='Active Subsidy',
      active=True,
      priority=5,
      reward_type='PERCENTAGE',
      reward_value=3.0
    )
    await sync_to_async(SubsidyRuleFactory)(
      name='Inactive Subsidy',
      active=False
    )
    
    response = await cast(Any, self.api_client.get("/"))
    
    self.assertEqual(response.status_code, 200)
    data = response.json()
    self.assertGreaterEqual(len(data), 1, "Should have at least 1 active rule")
    # Find our test rule in the response
    test_rule = next((r for r in data if r['name'] == 'Active Subsidy'), None)
    self.assertIsNotNone(test_rule, "Active Subsidy rule should be in response")
    self.assertEqual(test_rule['active'], True)
    self.assertEqual(test_rule['priority'], 5)
    self.assertEqual(test_rule['reward_type'], 'PERCENTAGE')
    self.assertEqual(test_rule['reward_value'], 3.0)
    
  async def test_subsidy_rules_privacy(self):
    """Test that internal budget fields are not exposed"""
    await sync_to_async(SubsidyRuleFactory)(active=True)
    
    response = await cast(Any, self.api_client.get("/"))
    data = response.json()[0]
    
    # Verify sensitive fields are NOT in response
    self.assertNotIn('allocation', data)
    self.assertNotIn('spent', data)


class MinistryAPITest(TestCase):
  """Test the /ministry/ endpoints"""
  def setUp(self):
    self.api_client = TestAsyncClient(ministry_router)

  async def test_get_current_ministry_term(self):
    """Test GET /ministry/current/ returns active term"""
    term = await sync_to_async(MinistryTermFactory)(is_active=True)
    
    response = await cast(Any, self.api_client.get("/current/"))
    
    self.assertEqual(response.status_code, 200)
    data = response.json()
    self.assertEqual(data['id'], term.id)
    self.assertEqual(data['minister_id'], str(term.minister.unique_id))
    self.assertEqual(data['is_active'], True)
    self.assertIn('initial_budget', data)
    self.assertIn('current_budget', data)
    self.assertIn('total_spent', data)
    self.assertIn('created_jobs_count', data)
    self.assertIn('expired_jobs_count', data)
  
  async def test_no_active_ministry_term(self):
    """Test GET /ministry/current/ returns None when no active term"""
    await sync_to_async(MinistryTermFactory)(is_active=False)
    
    response = await cast(Any, self.api_client.get("/current/"))
    
    self.assertEqual(response.status_code, 200)
    self.assertIsNone(response.json())


class ChampionshipsListAPITest(TestCase):
  """Test the /championships/ list endpoint"""
  def setUp(self):
    self.api_client = TestAsyncClient(championships_list_router)

  async def test_list_championships(self):
    """Test GET /championships/ returns all championships"""
    await sync_to_async(ChampionshipFactory)(name='Spring Cup')
    await sync_to_async(ChampionshipFactory)(name='Summer League')
    
    response = await cast(Any, self.api_client.get("/"))
    
    self.assertEqual(response.status_code, 200)
    data = response.json()
    self.assertEqual(len(data), 2)
    names = [c['name'] for c in data]
    self.assertIn('Spring Cup', names)
    self.assertIn('Summer League', names)


class DeliveryStatsAPITest(TestCase):
  """Test the /stats/deliveries/ endpoint"""
  def setUp(self):
    self.api_client = TestAsyncClient(deliveries_stats_router)

  async def test_delivery_stats_leaderboard(self):
    """Test GET /stats/deliveries/ returns aggregated stats"""
    character = await sync_to_async(CharacterFactory)()
    
    # Create 3 deliveries for same character
    await sync_to_async(DeliveryFactory)(
      character=character,
      quantity=10,
      payment=5000,
      subsidy=500,
      timestamp=timezone.now()
    )
    await sync_to_async(DeliveryFactory)(
      character=character,
      quantity=20,
      payment=10000,
      subsidy=1000,
      timestamp=timezone.now()
    )
    
    response = await cast(Any, self.api_client.get("/"))
    
    self.assertEqual(response.status_code, 200)
    data = response.json()
    self.assertEqual(len(data), 1)
    self.assertEqual(data[0]['character_id'], character.id)
    self.assertEqual(data[0]['character_name'], character.name)
    self.assertEqual(data[0]['total_deliveries'], 2)
    self.assertEqual(data[0]['total_payment'], 15000)
    self.assertEqual(data[0]['total_subsidy'], 1500)
    self.assertEqual(data[0]['total_quantity'], 30)
  
  async def test_delivery_stats_with_filters(self):
    """Test delivery stats respects limit and days parameters"""
    character1 = await sync_to_async(CharacterFactory)()
    character2 = await sync_to_async(CharacterFactory)()
    
    # Recent delivery
    await sync_to_async(DeliveryFactory)(
      character=character1,
      timestamp=timezone.now()
    )
    # Old delivery (should be excluded with days=1)
    await sync_to_async(DeliveryFactory)(
      character=character2,
      timestamp=timezone.now() - timezone.timedelta(days=10)
    )
    
    response = await cast(Any, self.api_client.get("/?days=1&limit=5"))
    
    self.assertEqual(response.status_code, 200)
    data = response.json()
    self.assertEqual(len(data), 1)
    self.assertEqual(data[0]['character_id'], character1.id)

  async def test_delivery_stats_privacy(self):
    """Test that player personal data is not exposed beyond character name"""
    character = await sync_to_async(CharacterFactory)()
    await sync_to_async(DeliveryFactory)(character=character, timestamp=timezone.now())
    
    response = await cast(Any, self.api_client.get("/"))
    data = response.json()[0]
    
    # Verify only safe fields are present
    self.assertIn('character_id', data)
    self.assertIn('character_name', data)
    self.assertIn('player_id', data)
    
    # Verify sensitive fields are NOT in response
    self.assertNotIn('discord_user_id', data)
    self.assertNotIn('money', data)
    self.assertNotIn('driver_level', data)
