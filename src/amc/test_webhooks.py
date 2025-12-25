import time
import asyncio
from datetime import timedelta
from unittest.mock import patch, MagicMock, AsyncMock
from django.test import TestCase
from django.contrib.gis.geos import Point
import unittest
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
  CharacterLocation,
  PlayerStatusLog,
  Delivery,
  SubsidyRule,
  Cargo,
)
from decimal import Decimal
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
    await DeliveryPoint.objects.acreate(guid="1", name="mine", type="mine", coord=Point(0,0,0))
    await DeliveryPoint.objects.acreate(guid="2", name="factory", type="factory", coord=Point(1000,1000,0))

    event = {
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


@patch('amc.webhook.get_rp_mode', new_callable=AsyncMock)
@patch('amc.webhook.get_treasury_fund_balance', new_callable=AsyncMock)
@patch('amc.webhook.announce', new_callable=AsyncMock)
@patch('amc.webhook.show_popup', new_callable=AsyncMock)
class ExtraWebhookTests(TestCase):
    async def test_cargo_aggregation_same_event(self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode):
        # Test that multiple cargos in one event are aggregated correctly
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player, guid="test-char-1")
        from amc.models import PlayerStatusLog
        await PlayerStatusLog.objects.acreate(
            character=character,
            timespan=(timezone.now() - timedelta(minutes=5), timezone.now())
        )
        await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")
        
        await DeliveryPoint.objects.acreate(guid="s1", name="S1", type="generic", coord=Point(0,0,0))
        await DeliveryPoint.objects.acreate(guid="d1", name="D1", type="generic", coord=Point(100,100,0))
        
        events = [{
            'hook': "ServerCargoArrived",
            'timestamp': int(time.time()),
            'data': {
                'CharacterGuid': str(character.guid),
                'Cargos': [
                    {
                        'Net_CargoKey': 'apples',
                        'Net_Payment': 1000,
                        'Net_Weight': 10.0,
                        'Net_Damage': 0.0,
                        'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0},
                        'Net_DestinationLocation': {'X': 100, 'Y': 100, 'Z': 0},
                    },
                    {
                        'Net_CargoKey': 'apples',
                        'Net_Payment': 1000,
                        'Net_Weight': 10.0,
                        'Net_Damage': 0.0,
                        'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0},
                        'Net_DestinationLocation': {'X': 100, 'Y': 100, 'Z': 0},
                    }
                ]
            }
        }]
        
        await process_events(events)
        
        # Should have 2 logs in ServerCargoArrivedLog but only 1 Delivery record (aggregated)
        self.assertEqual(await ServerCargoArrivedLog.objects.acount(), 2)
        self.assertEqual(await Delivery.objects.filter(character=character).acount(), 1)
        delivery = await Delivery.objects.afirst()
        self.assertEqual(delivery.quantity, 2)
        self.assertEqual(delivery.payment, 2000)

    async def test_shortcut_usage(self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode):
        # Test that subsidy is zeroed if shortcut was used
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player, guid="test-char-shortcut")
        await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")
        
        # Point inside gwangjin_shortcut
        await CharacterLocation.objects.acreate(
            character=character, 
            location=Point(359285, 892222, -3519),
            timestamp=timezone.now()
        )
        
        await DeliveryPoint.objects.acreate(guid="s1", name="S1", type="generic", coord=Point(0,0,0))
        await DeliveryPoint.objects.acreate(guid="d1", name="D1", type="generic", coord=Point(100,100,0))
        
        event = {
            'hook': "ServerCargoArrived",
            'timestamp': int(time.time()),
            'data': {
                'CharacterGuid': str(character.guid),
                'Cargos': [{
                    'Net_CargoKey': 'apples',
                    'Net_Payment': 1000,
                    'Net_Weight': 10.0,
                    'Net_Damage': 0.0,
                    'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0},
                    'Net_DestinationLocation': {'X': 100, 'Y': 100, 'Z': 0},
                }]
            }
        }
        
        # get_subsidy_for_cargo usually returns some subsidy. 
        # Let's ensure it would have a subsidy usually.
        payment, subsidy = await process_event(event, player, character)
        
        # With shortcut used, subsidy should be 0 and payment should not include it
        self.assertEqual(subsidy, 0)
        # The logic in process_events (plural) is:
        # if used_shortcut:
        #   total_payment -= total_subsidy
        #   total_subsidy = 0
        
        # Let's test via process_events to see the zeroing
        await CharacterLocation.objects.all().adelete()
        await PlayerStatusLog.objects.acreate(
            character=character,
            timespan=(timezone.now() - timedelta(minutes=5), timezone.now())
        )
        await CharacterLocation.objects.acreate(
            character=character, 
            location=Point(359285, 892222, -3519),
            timestamp=timezone.now()
        )
        
        # We need mock_get_rp_mode for process_events
        player_profits = []
        with patch('amc.webhook.on_player_profits', new_callable=AsyncMock) as mock_profits:
            await process_events([event], http_client_mod=MagicMock())
            # Yield to background tasks if any
            await asyncio.sleep(0)
            player_profits = mock_profits.call_args[0][0]
            
        # player_profits format: (character, total_subsidy, total_payment)
        char, total_subsidy, total_payment = player_profits[0]
        self.assertEqual(total_subsidy, 0)

    async def test_cargo_dumped(self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode):
        mock_get_rp_mode.return_value = False
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player, guid="test-char-dumped")
        await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")
        
        from amc.models import PlayerStatusLog
        await PlayerStatusLog.objects.acreate(
            character=character,
            timespan=(timezone.now() - timedelta(minutes=5), timezone.now())
        )
        
        event = {
            'hook': "ServerCargoDumped",
            'timestamp': int(time.time()),
            'data': {
                'PlayerId': str(player.unique_id),
                'Cargo': {
                    'Net_CargoKey': 'trash',
                    'Net_Payment': 500,
                    'Net_Weight': 50.0,
                    'Net_Damage': 0.1,
                }
            }
        }
        
        await process_events([event])
        
        self.assertEqual(await ServerCargoArrivedLog.objects.acount(), 1)
        log = await ServerCargoArrivedLog.objects.afirst()
        self.assertEqual(log.cargo_key, 'trash')
        self.assertEqual(log.payment, 500)

    async def test_vehicle_reset_rp_mode(self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode):
        mock_get_rp_mode.return_value = True
        player = await sync_to_async(PlayerFactory)()
        # Set last_login far enough in the past
        character = await sync_to_async(CharacterFactory)(
            player=player,
            guid="test-char-rp"
        )
        await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")
        from amc.models import PlayerStatusLog
        await PlayerStatusLog.objects.acreate(
            character=character,
            timespan=(timezone.now() - timedelta(minutes=5), timezone.now())
        )
        
        event = {
            'hook': "ServerResetVehicleAt",
            'timestamp': int(time.time()),
            'data': {
                'CharacterGuid': str(character.guid),
            }
        }
        
        await process_events([event])
        mock_announce.assert_called()
        self.assertIn("despawned", mock_announce.call_args[0][0])

    async def test_rp_mode_subsidy_fix(self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode):
        mock_get_rp_mode.return_value = True
        mock_get_treasury.return_value = 100_000
        
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player, guid="test-char-rp-fix")
        await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")
        
        p1 = await DeliveryPoint.objects.acreate(guid="s1", name="S1", type="generic", coord=Point(0,0,0))
        p2 = await DeliveryPoint.objects.acreate(guid="d1", name="D1", type="generic", coord=Point(100,100,0))

        event = {
            'hook': "ServerCargoArrived",
            'timestamp': int(time.time()),
            'data': {
                'CharacterGuid': str(character.guid),
                'Cargos': [
                    {
                        'Net_CargoKey': 'apples',
                        'Net_Payment': 1000,
                        'Net_Weight': 10.0,
                        'Net_Damage': 0.0,
                        'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0},
                        'Net_DestinationLocation': {'X': 100, 'Y': 100, 'Z': 0},
                    },
                    {
                        'Net_CargoKey': 'oranges',
                        'Net_Payment': 1000,
                        'Net_Weight': 10.0,
                        'Net_Damage': 0.0,
                        'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0},
                        'Net_DestinationLocation': {'X': 100, 'Y': 100, 'Z': 0},
                    }
                ]
            }
        }
        
        await process_events([event], http_client=MagicMock())
        
        deliveries = [d async for d in Delivery.objects.filter(character=character).order_by('id')]
        self.assertEqual(len(deliveries), 2)
        
        d1 = deliveries[0]
        d2 = deliveries[1]
        
        self.assertAlmostEqual(d1.subsidy, d2.subsidy, delta=1.0)

    @patch('amc.webhook.send_fund_to_player', new_callable=AsyncMock)
    async def test_proportional_job_rewards(self, mock_send_fund, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode):
        from amc.webhook import on_delivery_job_fulfilled
        
        p1 = await sync_to_async(PlayerFactory)()
        c1 = await sync_to_async(CharacterFactory)(player=p1, name="Alice")
        p2 = await sync_to_async(PlayerFactory)()
        c2 = await sync_to_async(CharacterFactory)(player=p2, name="Bob")
        
        job = await DeliveryJob.objects.acreate(
            name="Community Goal",
            quantity_requested=10,
            quantity_fulfilled=10,
            completion_bonus=10000,
            bonus_multiplier=1.0, # Added missing field
            expired_at=timezone.now() + timedelta(days=1),
        )
        
        # Alice delivered 7, Bob delivered 3
        await Delivery.objects.acreate(character=c1, job=job, quantity=7, timestamp=timezone.now(), payment=0, subsidy=0)
        await Delivery.objects.acreate(character=c2, job=job, quantity=3, timestamp=timezone.now(), payment=0, subsidy=0)
        
        await on_delivery_job_fulfilled(job, MagicMock())
        
        # Check Alice reward: 7/10 * 10000 = 7000
        # Check Bob reward: 3/10 * 10000 = 3000
        
        # send_fund_to_player(reward, character_obj, "Job Completion")
        fund_calls = mock_send_fund.call_args_list
        results = {call[0][1].id: call[0][0] for call in fund_calls}
        
        self.assertEqual(results[c1.id], 7000)
        self.assertEqual(results[c2.id], 3000)

    async def test_missing_character_skip(self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode):
        # Test that events for non-existent characters are skipped
        event = {
            'hook': "ServerCargoArrived",
            'timestamp': int(time.time()),
            'data': {
                'CharacterGuid': 'non-existent-guid',
                'PlayerId': '9999999',
                'Cargos': []
            }
        }
        
        # Should not raise exception
        await process_events([event])
        self.assertEqual(await ServerCargoArrivedLog.objects.acount(), 0)

    async def test_process_event_exception_triggers_popup(self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode):
        # Test that an exception in process_event triggers a popup
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player, guid="test-char-exception")
        await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")
        
        from amc.models import PlayerStatusLog
        await PlayerStatusLog.objects.acreate(
            character=character,
            timespan=(timezone.now() - timedelta(minutes=5), timezone.now())
        )
        
        event = {
            'hook': "ServerCargoArrived",
            'timestamp': int(time.time()),
            'data': {
                'CharacterGuid': str(character.guid),
                'Cargos': [{}] # This will cause an error in process_cargo_log (missing keys)
            }
        }
        
        with self.assertRaises(Exception):
            await process_events([event], http_client_mod=MagicMock())
        
        # Yield to allow the background task (show_popup) to run
        await asyncio.sleep(0.1)
        
        mock_show_popup.assert_called()
        self.assertIn("Webhook failed", mock_show_popup.call_args[0][1])

    @patch('amc.webhook.on_delivery_job_fulfilled')
    async def test_over_delivery_job_completion(self, mock_on_fulfilled, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player, guid="test-char-over")
        await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")
        
        p1 = await DeliveryPoint.objects.acreate(guid="s_over", name="S_Over", type="generic", coord=Point(0,0,0))
        p2 = await DeliveryPoint.objects.acreate(guid="d_over", name="D_Over", type="generic", coord=Point(100,100,0))
        
        # 1. Create a job with 29/30 fulfilled
        job = await DeliveryJob.objects.acreate(
            name="Over Delivery Job",
            # Cargo key must match the event
            cargo_key="apples",
            quantity_requested=30,
            quantity_fulfilled=29,
            completion_bonus=10000,
            bonus_multiplier=1.0,
            expired_at=timezone.now() + timedelta(days=1),
        )
        await job.source_points.aadd(p1)
        await job.destination_points.aadd(p2)
        
        # 2. Create dummy delivery for the 29 items so on_delivery_job_fulfilled can find them
        # (Though we are mocking on_delivery_job_fulfilled, so strictly speaking we verify it is CALLED.
        # But for correctness of the system state, let's create them)
        await Delivery.objects.acreate(
            character=character, 
            job=job, 
            quantity=29, 
            timestamp=timezone.now(), 
            payment=0, 
            subsidy=0,
            cargo_key="apples",
            sender_point=p1,
            destination_point=p2
        )
        
        # 3. Simulate existing delivery filling it to 29? 
        # Actually the job.quantity_fulfilled is 29. The Delivery rows support it. All good.
        
        # 4. Arrive with 10 more items
        event = {
            'hook': "ServerCargoArrived",
            'timestamp': int(time.time()),
            'data': {
                'CharacterGuid': str(character.guid),
                'Cargos': []
            }
        }
        # Create 10 cargos
        for _ in range(10):
            event['data']['Cargos'].append({
                'Net_CargoKey': 'apples',
                'Net_Payment': 100,
                'Net_Weight': 10.0,
                'Net_Damage': 0.0,
                'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0},
                'Net_DestinationLocation': {'X': 100, 'Y': 100, 'Z': 0},
            })
            
        await process_events([event], http_client=MagicMock())
        
        # 5. Verify logic
        await job.arefresh_from_db()
        self.assertEqual(job.quantity_fulfilled, 30, "Job should be capped at 30 fulfilled")
        
        # Verify on_delivery_job_fulfilled called exactly once
        self.assertEqual(mock_on_fulfilled.call_count, 1)

    @patch('amc.webhook.on_delivery_job_fulfilled')
    async def test_multi_job_completion(self, mock_on_fulfilled, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player, guid="test-char-multi")
        await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")
        
        p1 = await DeliveryPoint.objects.acreate(guid="s_multi", name="S_Multi", type="generic", coord=Point(0,0,0))
        p2 = await DeliveryPoint.objects.acreate(guid="d_multi", name="D_Multi", type="generic", coord=Point(100,100,0))
        
        # Job A: Apples
        job_a = await DeliveryJob.objects.acreate(
            name="Job A",
            cargo_key="apples",
            quantity_requested=10,
            quantity_fulfilled=9,
            completion_bonus=5000,
            bonus_multiplier=1.0,
            expired_at=timezone.now() + timedelta(days=1),
        )
        await job_a.source_points.aadd(p1)
        await job_a.destination_points.aadd(p2)
        
        # Job B: Oranges
        job_b = await DeliveryJob.objects.acreate(
            name="Job B",
            cargo_key="oranges",
            quantity_requested=10,
            quantity_fulfilled=9,
            completion_bonus=6000,
            bonus_multiplier=1.0,
            expired_at=timezone.now() + timedelta(days=1),
        )
        await job_b.source_points.aadd(p1)
        await job_b.destination_points.aadd(p2)
        
        # Event with 1 Apple and 1 Orange
        # Both should complete their respective jobs.
        event = {
            'hook': "ServerCargoArrived",
            'timestamp': int(time.time()),
            'data': {
                'CharacterGuid': str(character.guid),
                'Cargos': [
                    {
                        'Net_CargoKey': 'apples',
                        'Net_Payment': 100,
                        'Net_Weight': 10.0,
                        'Net_Damage': 0.0,
                        'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0},
                        'Net_DestinationLocation': {'X': 100, 'Y': 100, 'Z': 0},
                    },
                    {
                        'Net_CargoKey': 'oranges',
                        'Net_Payment': 100,
                        'Net_Weight': 10.0,
                        'Net_Damage': 0.0,
                        'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0},
                        'Net_DestinationLocation': {'X': 100, 'Y': 100, 'Z': 0},
                    }
                ]
            }
        }
            
        await process_events([event], http_client=MagicMock())
        
        await job_a.arefresh_from_db()
        await job_b.arefresh_from_db()
        
        self.assertEqual(job_a.quantity_fulfilled, 10, "Job A should be fulfilled")
        self.assertEqual(job_b.quantity_fulfilled, 10, "Job B should be fulfilled")
        
        # Verify on_delivery_job_fulfilled called TWICE (once for each job)
        # Note: on_delivery_job_fulfilled(job, client)
        self.assertEqual(mock_on_fulfilled.call_count, 2, "Both jobs should trigger completion hook")
        
        
        # Optional: check args to ensure both jobs were passed
        called_jobs = {call.args[0].id for call in mock_on_fulfilled.call_args_list}
        self.assertEqual(called_jobs, {job_a.id, job_b.id})

    @patch('amc.webhook.send_fund_to_player', new_callable=AsyncMock)
    async def test_job_completion_rewards_integration(self, mock_send_fund, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode):
        # This test DOES NOT mock on_delivery_job_fulfilled.
        # It verifies that the whole flow results in money being sent.
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000
        
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player, guid="test-char-reward")
        await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")
        
        p1 = await DeliveryPoint.objects.acreate(guid="s_rew", name="S_Rew", type="generic", coord=Point(0,0,0))
        p2 = await DeliveryPoint.objects.acreate(guid="d_rew", name="D_Rew", type="generic", coord=Point(100,100,0))
        
        job = await DeliveryJob.objects.acreate(
            name="Reward Job",
            cargo_key="gold",
            quantity_requested=10,
            quantity_fulfilled=9,
            completion_bonus=10000,
            bonus_multiplier=1.0,
            expired_at=timezone.now() + timedelta(days=1),
        )
        await job.source_points.aadd(p1)
        await job.destination_points.aadd(p2)
        
        # Helper to create existing delivery
        await Delivery.objects.acreate(
            character=character, 
            job=job, 
            quantity=9, 
            timestamp=timezone.now(), 
            payment=0, 
            subsidy=0, 
            cargo_key="gold",
            sender_point=p1,
            destination_point=p2
        )
        
        event = {
            'hook': "ServerCargoArrived",
            'timestamp': int(time.time()),
            'data': {
                'CharacterGuid': str(character.guid),
                'Cargos': [{
                    'Net_CargoKey': 'gold',
                    'Net_Payment': 100,
                    'Net_Weight': 10.0,
                    'Net_Damage': 0.0,
                    'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0},
                    'Net_DestinationLocation': {'X': 100, 'Y': 100, 'Z': 0},
                }]
            }
        }
        
        # We need to allow on_delivery_job_fulfilled to actually run, so we need to ensure it's imported
        # and NOT patched in this specific test method (or rather, we test the side effect: send_fund_to_player).
        # However, the CLASS might have patches. 
        # The class ExtraWebhookTests has patches on: get_rp_mode, get_treasury, announce, show_popup.
        # It DOES NOT patch on_delivery_job_fulfilled.
        
        await process_events([event], http_client=MagicMock())
        
        # Wait for background tasks
        await asyncio.sleep(0.1)
        
        await job.arefresh_from_db()
        self.assertIsNotNone(job.fulfilled_at)
        
        # Verify money was sent. 
        # Total contribution: 9 (existing) + 1 (new) = 10.
        # Reward = 10000.
        # Alice (character) should get 10000.
        
        self.assertEqual(mock_send_fund.call_count, 1, "Should send funds exactly once")
        self.assertEqual(mock_send_fund.call_args[0][0], 10000, "Should send 10000 reward")


@patch('amc.webhook.get_rp_mode', new_callable=AsyncMock)
@patch('amc.webhook.get_treasury_fund_balance', new_callable=AsyncMock)
@patch('amc.webhook.announce', new_callable=AsyncMock)
@patch('amc.webhook.show_popup', new_callable=AsyncMock)
class SubsidyIntegrationTests(TestCase):
    async def test_subsidy_rule_integration(self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000_000
        
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player, guid="test-char-subsidy")
        await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")

        # Create Cargo
        cargo_apple, _ = await Cargo.objects.aget_or_create(key="apples", defaults={"label": "Apples"})

        # Create Subsidy Rule
        rule = await SubsidyRule.objects.acreate(
            name="Apple Subsidy",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("2.0"), # 200%
            active=True,
            priority=10,
            allocation=Decimal("100000"),
            spent=Decimal("0")
        )
        await rule.cargos.aadd(cargo_apple)
        
        # Delivery Points
        p1 = await DeliveryPoint.objects.acreate(guid="s1", name="S1", type="generic", coord=Point(0,0,0))
        p2 = await DeliveryPoint.objects.acreate(guid="d1", name="D1", type="generic", coord=Point(100,100,0))
        
        event = {
            'hook': "ServerCargoArrived",
            'timestamp': int(time.time()),
            'data': {
                'CharacterGuid': str(character.guid),
                'Cargos': [{
                    'Net_CargoKey': 'apples',
                    'Net_Payment': 1000,
                    'Net_Weight': 10.0,
                    'Net_Damage': 0.0,
                    'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0},
                    'Net_DestinationLocation': {'X': 100, 'Y': 100, 'Z': 0},
                }]
            }
        }
        
        # Run process_events
        await process_events([event], http_client=MagicMock())
        
        # Verify Delivery
        delivery = await Delivery.objects.filter(character=character).afirst()
        self.assertIsNotNone(delivery, "Delivery should be created")
        
        # Expected subsidy: 1000 * 2.0 = 2000
        self.assertEqual(delivery.subsidy, 2000, "Subsidy should be calculated correctly based on Rule")
        
        # Verify Rule Spent updated
        await rule.arefresh_from_db()
        self.assertEqual(rule.spent, 2000, "SubsidyRule.spent should be updated")

    @unittest.skip("Allocation limit logic temporarily disabled")
    async def test_subsidy_allocation_limit_integration(self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode):
        # Test that subsidy is capped by allocation
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000_000
        
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player, guid="test-char-capped")
        await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")

        cargo_gold, _ = await Cargo.objects.aget_or_create(key="gold", defaults={"label": "Gold"})

        # Rule with only 500 remaining allocation
        # allocation=2000, spent=1500 -> remaining=500
        rule = await SubsidyRule.objects.acreate(
            name="Gold Subsidy",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("1.0"), # 100%
            active=True,
            priority=10,
            allocation=Decimal("2000"),
            spent=Decimal("1500")
        )
        await rule.cargos.aadd(cargo_gold)
        
        p1 = await DeliveryPoint.objects.acreate(guid="s1", name="S1", type="generic", coord=Point(0,0,0))
        p2 = await DeliveryPoint.objects.acreate(guid="d1", name="D1", type="generic", coord=Point(100,100,0))
        
        event = {
            'hook': "ServerCargoArrived",
            'timestamp': int(time.time()),
            'data': {
                'CharacterGuid': str(character.guid),
                'Cargos': [{
                    'Net_CargoKey': 'gold',
                    'Net_Payment': 1000, # with 100% subsidy should be 1000 subsidy
                    'Net_Weight': 10.0,
                    'Net_Damage': 0.0,
                    'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0},
                    'Net_DestinationLocation': {'X': 100, 'Y': 100, 'Z': 0},
                }]
            }
        }
        
        await process_events([event], http_client=MagicMock())
        
        delivery = await Delivery.objects.filter(character=character).afirst()
        self.assertIsNotNone(delivery)
        
        # Subsidy should be capped at 500 (remaining allocation)
        self.assertEqual(delivery.subsidy, 500, "Subsidy should be capped by rule allocation")
        
        await rule.arefresh_from_db()
        self.assertEqual(rule.spent, 2000, "Rule should now be fully spent")

    async def test_delivery_point_tolerance(self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode):
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000_000
        
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player, guid="test-char-tolerance")
        await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")

        cargo_coal, _ = await Cargo.objects.aget_or_create(key="coal", defaults={"label": "Coal"})

        # Rule requires specific source point
        # Point is at (0,0)
        p_zero = await DeliveryPoint.objects.acreate(guid="p0", name="Zero Point", type="generic", coord=Point(0,0,0))
        
        rule = await SubsidyRule.objects.acreate(
            name="Proximity Rule",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("1.0"),
            active=True,
            priority=10,
            allocation=Decimal("100000")
        )
        await rule.cargos.aadd(cargo_coal)
        await rule.source_delivery_points.aadd(p_zero)
        # Note: SubsidyRule checks filter(Q(source_delivery_points__coord__dwithin=(cargo.sender_point.coord, 1.0)))
        # AND Filtering for the Delivery Point itself (Webhook logic) uses buffer(1)
        
        # 1. Test EXACT Location -> Should Match
        event_exact = {
            'hook': "ServerCargoArrived",
            'timestamp': int(time.time()),
            'data': {
                'CharacterGuid': str(character.guid),
                'Cargos': [{
                    'Net_CargoKey': 'coal',
                    'Net_Payment': 100,
                    'Net_Weight': 10.0,
                    'Net_Damage': 0.0,
                    'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0},
                    'Net_DestinationLocation': {'X': 100, 'Y': 100, 'Z': 0},
                }]
            }
        }
        await process_events([event_exact], http_client=MagicMock())
        d1 = await Delivery.objects.filter(character=character).order_by('-id').afirst()
        self.assertEqual(d1.subsidy, 100, "Exact match should get subsidy")

        # 2. Test Within Tolerance (0.9) -> Should Match
        # But wait! Webhook 'process_cargo_log' logic:
        # sender_coord = Point(X,Y,Z).buffer(1)
        # sender = DeliveryPoint.objects.filter(coord__coveredby=sender_coord).afirst()
        # If I send (0.9, 0, 0), Point(0.9,0,0).buffer(1) creates circle from -0.1 to 1.9.
        # Zero point (0,0,0) IS covered by that circle.
        # So "sender_point" on the Application Log will be resolved essentially?
        # YES. If resolved, then SubsidyRule uses `cargo.sender_point` (which is the DB object).
        # So if webhook resolves it, SubsidyRule sees the DB object.
        
        event_near = {
            'hook': "ServerCargoArrived",
            'timestamp': int(time.time()),
            'data': {
                'CharacterGuid': str(character.guid),
                'Cargos': [{
                    'Net_CargoKey': 'coal',
                    'Net_Payment': 100,
                    'Net_Weight': 10.0,
                    'Net_Damage': 0.0,
                    'Net_SenderAbsoluteLocation': {'X': 0.9, 'Y': 0, 'Z': 0},
                    'Net_DestinationLocation': {'X': 100, 'Y': 100, 'Z': 0},
                }]
            }
        }
        await process_events([event_near], http_client=MagicMock())
        d2 = await Delivery.objects.filter(character=character).order_by('-id').afirst()
        self.assertEqual(d2.subsidy, 100, "0.9 distance should resolve point and get subsidy")
        
        # 3. Test Outside Tolerance (1.1) -> Should NOT Match
        # Point(1.1, 0, 0).buffer(1) -> Circle from 0.1 to 2.1.
        # Zero point (0,0,0) is NOT covered. 
        # So 'sender_point' will be None.
        # SubsidyRule loop: if cargo.sender_point is None:
        #   rules = rules.filter(source_areas__isnull=True, source_delivery_points__isnull=True)
        # Our rule HAS source_delivery_points, so it should be filtered out.
        
        event_far = {
            'hook': "ServerCargoArrived",
            'timestamp': int(time.time()),
            'data': {
                'CharacterGuid': str(character.guid),
                'Cargos': [{
                    'Net_CargoKey': 'coal',
                    'Net_Payment': 100,
                    'Net_Weight': 10.0,
                    'Net_Damage': 0.0,
                    'Net_SenderAbsoluteLocation': {'X': 1.1, 'Y': 0, 'Z': 0},
                    'Net_DestinationLocation': {'X': 100, 'Y': 100, 'Z': 0},
                }]
            }
        }
        await process_events([event_far], http_client=MagicMock())
        d3 = await Delivery.objects.filter(character=character).order_by('-id').afirst()
        self.assertEqual(d3.subsidy, 0, "1.1 distance should NOT resolve point and thus NOT get subsidy")

    async def test_subsidy_zero_treasury(self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode):
        # Verify that 0 treasury results in 0 subsidy
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 0
        
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player, guid="test-char-zero-treasury")
        await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")
        
        cargo, _ = await Cargo.objects.aget_or_create(key="coal", defaults={"label": "Coal"})
        p_start = await DeliveryPoint.objects.acreate(guid="pS", name="Start", type="generic", coord=Point(0,0,0))
        
        rule = await SubsidyRule.objects.acreate(
            name="Treasury Check",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("1.0"),
            active=True,
            priority=10,
            allocation=Decimal("100000")
        )
        await rule.cargos.aadd(cargo)
        await rule.source_delivery_points.aadd(p_start)
        
        event = {
            'hook': "ServerCargoArrived",
            'timestamp': int(time.time()),
            'data': {
                'CharacterGuid': str(character.guid),
                'Cargos': [{
                    'Net_CargoKey': 'coal',
                    'Net_Payment': 1000,
                    'Net_Weight': 10.0,
                    'Net_Damage': 0.0,
                    'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0}, # Matches p_start
                    'Net_DestinationLocation': {'X': 100, 'Y': 100, 'Z': 0},
                }]
            }
        }
        await process_events([event], http_client=MagicMock())
        d = await Delivery.objects.filter(character=character).afirst()
        self.assertEqual(d.subsidy, 0, "Zero treasury should result in zero subsidy")

    async def test_subsidy_cargo_case_mismatch(self, mock_show_popup, mock_announce, mock_get_treasury, mock_get_rp_mode):
        # Verify if 'Coal' vs 'coal' mismatch causes 0 subsidy
        mock_get_rp_mode.return_value = False
        mock_get_treasury.return_value = 100_000_000
        
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player, guid="test-char-case")
        await CharacterLocation.objects.acreate(character=character, location=Point(0,0,0), vehicle_key="TestVehicle")
        
        # Rule uses 'Coal' (Title Case)
        cargo, _ = await Cargo.objects.aget_or_create(key="Coal", defaults={"label": "Coal"})
        
        rule = await SubsidyRule.objects.acreate(
            name="Case Sensitive Rule",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("1.0"),
            active=True,
            priority=10,
            allocation=Decimal("100000")
        )
        await rule.cargos.aadd(cargo)
        
        # Event uses 'coal' (lower case)
        event = {
            'hook': "ServerCargoArrived",
            'timestamp': int(time.time()),
            'data': {
                'CharacterGuid': str(character.guid),
                'Cargos': [{
                    'Net_CargoKey': 'coal',
                    'Net_Payment': 1000,
                    'Net_Weight': 10.0,
                    'Net_Damage': 0.0,
                    'Net_SenderAbsoluteLocation': {'X': 0, 'Y': 0, 'Z': 0},
                    'Net_DestinationLocation': {'X': 100, 'Y': 100, 'Z': 0},
                }]
            }
        }
        await process_events([event], http_client=MagicMock())
        d = await Delivery.objects.filter(character=character).afirst()
        
        # If strict matching, this should be 0.
        # Note: In Python, string equality is case sensitive. In Postgres, defaults are too.
        # This test ensures we KNOW if it's failing due to case.
        self.assertEqual(d.subsidy, 0, "Case mismatch should result in zero subsidy (if strictly matched)")
