import time
from django.test import TestCase
from asgiref.sync import sync_to_async
from amc.factories import PlayerFactory
from amc.webhook import process_events, process_event
from amc.models import (
  ServerCargoArrivedLog,
  ServerPassengerArrivedLog,
  ServerTowRequestArrivedLog,
)

class ProcessEventTests(TestCase):
  async def test_process_event(self):
    player = await sync_to_async(PlayerFactory)()
    event = {
      'hook': "/Script/MotorTown.MotorTownPlayerController:ServerCargoArrived",
      'timestamp': int(time.time() * 1000),
      'data': {
        'Cargos': [
          {
            'Net_CargoKey': 'oranges',
            'Net_Payment': {
              'BaseValue': 10_000,
              'ShadowedValue': 10_000,
            },
            'Net_Weight': 100.0,
            'Net_Damage': 0.0,
          }
        ],
        'PlayerId': str(player.unique_id),
      }
    }
    payment, subsidy = await process_event(event, player)
    self.assertEqual(
      await ServerCargoArrivedLog.objects.acount(),
      1
    )
    delivery = await ServerCargoArrivedLog.objects.select_related('player').afirst()
    self.assertEqual(delivery.payment, 10_000)
    self.assertEqual(payment, 10_000)
    self.assertEqual(delivery.cargo_key, 'oranges')
    self.assertEqual(delivery.weight, 100.0)
    self.assertEqual(delivery.damage, 0.0)
    self.assertEqual(delivery.player, player)

  async def test_taxi(self):
    player = await sync_to_async(PlayerFactory)()
    event = {
      'hook': "/Script/MotorTown.MotorTownPlayerController:ServerPassengerArrived",
      'timestamp': int(time.time() * 1000),
      'data': {
        'Passenger': {
          'Net_PassengerType': 2,
          'Net_Payment': 10_000,
          'Net_bArrived': True,
          'Net_Distance': 10_000,
        },
        'PlayerId': str(player.unique_id),
      }
    }
    payment, subsidy = await process_event(event, player)
    self.assertEqual(
      await ServerPassengerArrivedLog.objects.acount(),
      1
    )
    log = await ServerPassengerArrivedLog.objects.select_related('player').afirst()
    self.assertEqual(log.payment, 10_000)
    self.assertEqual(payment, 17_000)
    self.assertEqual(subsidy, 7_000)
    self.assertEqual(log.player, player)

  async def test_tow(self):
    player = await sync_to_async(PlayerFactory)()
    event = {
      'hook': "/Script/MotorTown.MotorTownPlayerController:ServerTowRequestArrived",
      'timestamp': int(time.time() * 1000),
      'data': {
        'TowRequest': {
          'Net_TowRequestFlags': 1,
          'Net_Payment': 10_000,
        },
        'PlayerId': str(player.unique_id),
      }
    }
    payment, subsidy = await process_event(event, player)
    self.assertEqual(
      await ServerTowRequestArrivedLog.objects.acount(),
      1
    )
    log = await ServerTowRequestArrivedLog.objects.select_related('player').afirst()
    self.assertEqual(log.payment, 10_000)
    self.assertEqual(payment, 27_000)
    self.assertEqual(subsidy, 17_000)
    self.assertEqual(log.player, player)

class ProcessEventsTests(TestCase):
  async def test_process_events(self):
    player1 = await sync_to_async(PlayerFactory)()
    player2 = await sync_to_async(PlayerFactory)()
    events = [{
      'hook': "/Script/MotorTown.MotorTownPlayerController:ServerCargoArrived",
      'timestamp': int(time.time() * 1000),
      'data': {
        'Cargos': [
          {
            'Net_CargoKey': 'oranges',
            'Net_Payment': {
              'BaseValue': 10_000,
              'ShadowedValue': 10_000,
            },
            'Net_Weight': 100.0,
            'Net_Damage': 0.0,
          }
        ],
        'PlayerId': str(player1.unique_id),
      }
    }, {
      'hook': "/Script/MotorTown.MotorTownPlayerController:ServerCargoArrived",
      'timestamp': int(time.time() * 1000),
      'data': {
        'Cargos': [
          {
            'Net_CargoKey': 'oranges',
            'Net_Payment': {
              'BaseValue': 10_000,
              'ShadowedValue': 10_000,
            },
            'Net_Weight': 100.0,
            'Net_Damage': 0.0,
          }
        ],
        'PlayerId': str(player1.unique_id),
      }
    }, {
      'hook': "/Script/MotorTown.MotorTownPlayerController:ServerCargoArrived",
      'timestamp': int(time.time() * 1000),
      'data': {
        'Cargos': [
          {
            'Net_CargoKey': 'oranges',
            'Net_Payment': {
              'BaseValue': 10_000,
              'ShadowedValue': 10_000,
            },
            'Net_Weight': 100.0,
            'Net_Damage': 0.0,
          }
        ],
        'PlayerId': str(player2.unique_id),
      }
    }, ]
    await process_events(events)
    self.assertEqual(
      await ServerCargoArrivedLog.objects.acount(),
      3
    )
