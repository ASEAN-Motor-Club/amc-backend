import time
from datetime import timedelta
from unittest.mock import patch, MagicMock, AsyncMock
from django.test import TestCase
from django.contrib.gis.geos import Point
from asgiref.sync import sync_to_async
from amc.factories import PlayerFactory, CharacterFactory
from amc.webhook import process_events, process_event
from amc.models import (
  DeliveryPoint,
  ServerCargoArrivedLog,
  ServerPassengerArrivedLog,
  ServerTowRequestArrivedLog,
  DeliveryJob,
  ServerSignContractLog,
  Character,
  CharacterLocation,
)
from django.utils import timezone

@patch('amc.webhook.get_rp_mode', new_callable=AsyncMock)
@patch('amc.webhook.get_treasury_fund_balance', new_callable=AsyncMock)
class ProcessEventTests(TestCase):
  async def test_process_event(self, mock_get_treasury, mock_get_rp_mode):
    mock_get_rp_mode.return_value = False
    mock_get_treasury.return_value = 100_000

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
    await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")
    
    mine = await DeliveryPoint.objects.acreate(
      guid="1",
      name="mine",
      type="mine",
      coord=Point(0,0,0),
    )
    factory = await DeliveryPoint.objects.acreate(
      guid="2",
      name="factory",
      type="factory",
      coord=Point(1000,1000,0),
    )
    event = {
      'hook': "ServerCargoArrived",
      'timestamp': int(time.time()),
      'data': {
        'Cargos': [
          {
            'Net_CargoKey': 'oranges',
            'Net_Payment': 10_000,
            'Net_Weight': 100.0,
            'Net_Damage': 0.0,
            'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0},
            'Net_DestinationLocation': {'X': 1000, 'Y': 1000, 'Z': 0},
          }
        ],
        'PlayerId': str(player.unique_id),
        'CharacterGuid': str(character.guid),
      }
    }
    payment, subsidy = await process_event(event, player, character)
    self.assertEqual(
      await ServerCargoArrivedLog.objects.acount(),
      1
    )
    delivery = await ServerCargoArrivedLog.objects.select_related('player', 'sender_point', 'destination_point').afirst()
    self.assertEqual(delivery.payment, 10_000)
    self.assertEqual(payment, 10_000)
    self.assertEqual(delivery.cargo_key, 'oranges')
    self.assertEqual(delivery.weight, 100.0)
    self.assertEqual(delivery.damage, 0.0)
    self.assertEqual(delivery.player, player)
    self.assertEqual(delivery.sender_point, mine)
    self.assertEqual(delivery.destination_point, factory)

  async def test_taxi(self, mock_get_treasury, mock_get_rp_mode):
    mock_get_rp_mode.return_value = False
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
    await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")
    
    event = {
      'hook': "ServerPassengerArrived",
      'timestamp': int(time.time()),
      'data': {
        'Passenger': {
          'Net_PassengerType': 2,
          'Net_Payment': 10_000,
          'Net_bArrived': True,
          'Net_Distance': 10_000,
          'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0},
          'Net_DestinationLocation': {'X': 1000, 'Y': 1000, 'Z': 0},
          'Net_LCComfortSatisfaction': 5,
          'Net_TimeLimitPoint': 5,
        },
        'PlayerId': str(player.unique_id),
      }
    }
    payment, subsidy = await process_event(event, player, character)
    self.assertEqual(
      await ServerPassengerArrivedLog.objects.acount(),
      1
    )
    log = await ServerPassengerArrivedLog.objects.select_related('player').afirst()
    self.assertEqual(log.payment, 10_000)
    self.assertEqual(payment, 17_000)
    self.assertEqual(subsidy, 7_000)
    self.assertEqual(log.player, player)

  async def test_tow(self, mock_get_treasury, mock_get_rp_mode):
    mock_get_rp_mode.return_value = False
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
    await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")

    event = {
      'hook': "ServerTowRequestArrived",
      'timestamp': int(time.time()),
      'data': {
        'TowRequest': {
          'Net_TowRequestFlags': 1,
          'Net_Payment': 10_000,
        },
        'PlayerId': str(player.unique_id),
      }
    }
    payment, subsidy = await process_event(event, player, character)
    self.assertEqual(
      await ServerTowRequestArrivedLog.objects.acount(),
      1
    )
    log = await ServerTowRequestArrivedLog.objects.select_related('player').afirst()
    self.assertEqual(log.payment, 10_000)
    self.assertEqual(payment, 22_000)
    self.assertEqual(subsidy, 12_000)
    self.assertEqual(log.player, player)
    
  async def test_rp_mode_subsidy(self, mock_get_treasury, mock_get_rp_mode):
    # Verify subsidy calculation when RP mode is ON
    mock_get_rp_mode.return_value = True
    mock_get_treasury.return_value = 100_000

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
    await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")
    
    # Needs points for delivery creation
    mine = await DeliveryPoint.objects.acreate(guid="1", name="mine", type="mine", coord=Point(0,0,0))
    factory = await DeliveryPoint.objects.acreate(guid="2", name="factory", type="factory", coord=Point(1000,1000,0))

    event = {
      'hook': "ServerCargoArrived", # Using simplified hook name for process_event internal logic match if needed, but integration uses full string. process_event uses exact string from event['hook'].
      # Wait, process_events does grouping and passes individual events. 
      # The hook in process_event match is "ServerCargoArrived". 
      # In the original file, it matches `case "ServerCargoArrived":`.
      'hook': "ServerCargoArrived", 
      'timestamp': int(time.time()),
      'data': {
        'Cargos': [{
            'Net_CargoKey': 'oranges',
            'Net_Payment': 10_000,
            'Net_Weight': 100.0,
            'Net_Damage': 0.0,
            'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0},
            'Net_DestinationLocation': {'X': 1000, 'Y': 1000, 'Z': 0},
        }],
        'PlayerId': str(player.unique_id),
      }
    }
    
    payment, subsidy = await process_event(event, player, character, is_rp_mode=True, treasury_balance=100_000)
    
    self.assertEqual(subsidy, 5000)
    self.assertEqual(payment, 15000)


  async def test_job_completion(self, mock_get_treasury, mock_get_rp_mode):
      mock_get_rp_mode.return_value = False
      
      player = await sync_to_async(PlayerFactory)()
      character = await sync_to_async(CharacterFactory)(player=player)
      await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")
      
      p1 = await DeliveryPoint.objects.acreate(guid="j1", name="J1", type="generic", coord=Point(0,0,0))
      p2 = await DeliveryPoint.objects.acreate(guid="j2", name="J2", type="generic", coord=Point(100,100,0))
      
      job = await DeliveryJob.objects.acreate(
          name="Test Job",
          cargo_key="apples",
          quantity_requested=10,
          quantity_fulfilled=0,
          completion_bonus=50000,
          bonus_multiplier=1.0,
          expired_at=timezone.now() + timedelta(days=1),
      )
      await job.source_points.aadd(p1)
      await job.destination_points.aadd(p2)
      
      event = {
        'hook': "ServerCargoArrived",
        'timestamp': int(time.time()),
        'data': {
          'Cargos': [{
              'Net_CargoKey': 'apples',
              'Net_Payment': 100,
              'Net_Weight': 10.0,
              'Net_Damage': 0.0,
              'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0},
              'Net_DestinationLocation': {'X': 100, 'Y': 100, 'Z': 0},
          }],
          'PlayerId': str(player.unique_id),
        }
      }
      
      await process_event(event, player, character)
      
      await job.arefresh_from_db()
      self.assertEqual(job.quantity_fulfilled, 1)

  async def test_server_sign_contract(self, mock_get_treasury, mock_get_rp_mode):
      player = await sync_to_async(PlayerFactory)()
      character = await sync_to_async(CharacterFactory)(player=player)
      await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")
      
      event = {
          'hook': "ServerSignContract",
          'timestamp': int(time.time()),
          'data': {
              'Contract': {
                  'Item': 'sand',
                  'Amount': 100,
                  'CompletionPayment': {'BaseValue': 50000},
                  'Cost': {'BaseValue': 1000}
              }
          }
      }
      
      await process_event(event, player, character)
      
      self.assertEqual(await ServerSignContractLog.objects.acount(), 1)
      log = await ServerSignContractLog.objects.afirst()
      self.assertEqual(log.cargo_key, 'sand')
      self.assertEqual(log.amount, 100)
      self.assertEqual(log.payment, 50000)
      self.assertEqual(log.cost, 1000)

  async def test_contract_delivered(self, mock_get_treasury, mock_get_rp_mode):
      player = await sync_to_async(PlayerFactory)()
      character = await sync_to_async(CharacterFactory)(player=player)
      await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")
      
      # Create initial contract log
      log = await ServerSignContractLog.objects.acreate(
          guid="contract_guid_123",
          player=player,
          cargo_key="sand",
          amount=2,
          finished_amount=0,
          payment=50000,
          cost=1000,
          timestamp=timezone.now()
      )
      
      event = {
          'hook': "ServerContractCargoDelivered",
          'timestamp': int(time.time()),
          'data': {
              'ContractGuid': "contract_guid_123",
              'Item': 'sand',
              'Amount': 2,
              'CompletionPayment': 50000, 
              'Cost': 1000
          }
      }
      
      # First delivery
      await process_event(event, player, character)
      await log.arefresh_from_db()
      self.assertEqual(log.finished_amount, 1)
      self.assertFalse(log.delivered)
      
      # Second delivery (completion)
      await process_event(event, player, character)
      await log.arefresh_from_db()
      self.assertEqual(log.finished_amount, 2)
      self.assertTrue(log.delivered)


@patch('amc.webhook.get_rp_mode', new_callable=AsyncMock)
@patch('amc.webhook.get_treasury_fund_balance', new_callable=AsyncMock)
class ProcessEventsTests(TestCase):
  async def test_process_events_integration(self, mock_get_treasury, mock_get_rp_mode):
    mock_get_rp_mode.return_value = False
    mock_get_treasury.return_value = 100_000
    
    player1 = await sync_to_async(PlayerFactory)()
    character1 = await sync_to_async(CharacterFactory)(player=player1, guid="char1")
    await CharacterLocation.objects.acreate(character=character1, location=Point(0,0,0), vehicle_key="TestVehicle1")
    player2 = await sync_to_async(PlayerFactory)()
    character2 = await sync_to_async(CharacterFactory)(player=player2, guid="char2")
    await CharacterLocation.objects.acreate(character=character2, location=Point(0,0,0), vehicle_key="TestVehicle2")

    # Mocks for clients
    http_client = AsyncMock()
    http_client_mod = MagicMock()
    
    # Configure post to return an async context manager
    post_context = AsyncMock()
    post_context.__aenter__.return_value = MagicMock(status=200)
    post_context.__aexit__.return_value = None
    http_client_mod.post.return_value = post_context

    # Ensure get also works if needed (though get_rp_mode is patched)
    get_context = AsyncMock()
    get_context.__aenter__.return_value = MagicMock(status=200)
    get_context.__aexit__.return_value = None
    http_client_mod.get.return_value = get_context
    discord_client = AsyncMock()
    
    events = [{
      'hook': "ServerCargoArrived",
      'timestamp': int(time.time()),
      'data': {
        'Cargos': [
          {
            'Net_CargoKey': 'oranges',
            'Net_Payment': 10000,
            'Net_Weight': 100.0,
            'Net_Damage': 0.0,
            'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0},
            'Net_DestinationLocation': {'X': 1000, 'Y': 1000, 'Z': 0},
          }
        ],
        'CharacterGuid': str(character1.guid),
      }
    }, {
      'hook': "ServerCargoArrived",
      'timestamp': int(time.time()),
      'data': {
        'Cargos': [
          {
            'Net_CargoKey': 'oranges',
            'Net_Payment': 10000,
            'Net_Weight': 100.0,
            'Net_Damage': 0.0,
            'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0},
            'Net_DestinationLocation': {'X': 1000, 'Y': 1000, 'Z': 0},
          }
        ],
        'CharacterGuid': str(character1.guid),
      }
    }, {
      'hook': "ServerCargoArrived",
      'timestamp': int(time.time()),
      'data': {
        'Cargos': [
          {
            'Net_CargoKey': 'oranges',
            'Net_Payment': 10000,
            'Net_Weight': 100.0,
            'Net_Damage': 0.0,
            'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0},
            'Net_DestinationLocation': {'X': 1000, 'Y': 1000, 'Z': 0},
          }
        ],
        'CharacterGuid': str(character2.guid),
      }
    }]
    
    await DeliveryPoint.objects.acreate(guid="1", name="mine", type="mine", coord=Point(0,0,0))
    await DeliveryPoint.objects.acreate(guid="2", name="factory", type="factory", coord=Point(1000,1000,0))

    await process_events(events, http_client, http_client_mod, discord_client)
    
    self.assertEqual(
      await ServerCargoArrivedLog.objects.acount(),
      3
    )
    
    mock_jobs_cog = MagicMock()
    mock_jobs_cog.post_delivery_embed = AsyncMock()
    discord_client.get_cog.return_value = mock_jobs_cog
    
    await process_events(events[:1], http_client, http_client_mod, discord_client)
