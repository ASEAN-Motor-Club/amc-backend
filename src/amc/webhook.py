import asyncio
import itertools
from datetime import datetime
from django.utils import timezone
from django.contrib.gis.geos import Point
from django.db.models import F
from amc.mod_server import get_webhook_events, show_popup
from amc.subsidies import (
  repay_loan_for_profit,
  set_aside_player_savings,
  get_subsidy_for_cargo,
  get_subsidy_for_cargos,
  get_passenger_subsidy,
  subsidise_player,
)
from amc.models import (
  Player,
  ServerCargoArrivedLog,
  ServerSignContractLog,
  ServerPassengerArrivedLog,
  ServerTowRequestArrivedLog,
  DeliveryPoint,
)


async def on_player_profits(player_profits, session):
  for player, total_subsidy, total_payment in player_profits:
    if total_subsidy != 0:
      await subsidise_player(total_subsidy, player, session)
      await asyncio.sleep(0.2)
    loan_repayment = await repay_loan_for_profit(player, total_payment, session)
    savings = total_payment - loan_repayment
    if savings > 0:
      await set_aside_player_savings(player, savings, session)
      await asyncio.sleep(0.2)


async def monitor_webhook(ctx):
  http_client_mod = ctx.get('http_client_mod')
  events = await get_webhook_events(http_client_mod)
  await process_events(events, http_client_mod)


async def process_events(events, http_client_mod=None):
  def key_fn(event):
    return (event['data']['PlayerId'], event['hook'])

  sorted_events = sorted(events, key=key_fn)
  grouped_events = itertools.groupby(sorted_events, key=key_fn)
  aggregated_events = []

  for key, group in grouped_events:
    group_events = list(group)
    match key[1]:
      case "/Script/MotorTown.MotorTownPlayerController:ServerCargoArrived":
        cargos = [
          cargo
          for event in group_events
          for cargo in event['data']['Cargos']
        ]
        aggregated_events.append({
          'hook': key[1],
          'timestamp': group_events[0]['timestamp'],
          'data': {
            'PlayerId': key[0],
            'Cargos': cargos,
          }
        })
      case _:
        aggregated_events.extend(group_events)


  def key_by_player(event):
    return event['data']['PlayerId']

  sorted_player_events = sorted(aggregated_events, key=key_by_player)
  grouped_player_events = itertools.groupby(sorted_player_events, key=key_by_player)

  player_profits = []
  for player_id, es in grouped_player_events:
    try:
      player = await Player.objects.aget(unique_id=player_id)
    except Player.DoesNotExist:
      continue

    total_payment = 0
    total_subsidy = 0

    for event in es:
      try:
        payment, subsidy = await process_event(event, player)
        total_payment += payment
        total_subsidy += subsidy
      except Exception as e:
        asyncio.create_task(
          show_popup(http_client_mod, f"Webhook failed, please send to discord:\n{e}", player_id=player_id)
        )
        raise e

    player_profits.append((player, total_subsidy, total_payment))

  if http_client_mod:
    asyncio.create_task(
      on_player_profits(player_profits, http_client_mod)
    )

async def process_cargo_log(cargo, player, timestamp):
  sender_coord_raw = cargo['Net_SenderAbsoluteLocation']
  sender_coord = Point(
    sender_coord_raw['X'],
    sender_coord_raw['Y'],
    sender_coord_raw['Z'],
  ).buffer(1)
  destination_coord_raw = cargo['Net_DestinationLocation']
  destination_coord = Point(
    destination_coord_raw['X'],
    destination_coord_raw['Y'],
    destination_coord_raw['Z'],
  ).buffer(1)
  sender = await DeliveryPoint.objects.filter(coord__coveredby=sender_coord).afirst()
  destination = await DeliveryPoint.objects.filter(coord__coveredby=destination_coord).afirst()
  return ServerCargoArrivedLog(
    timestamp=timestamp,
    player=player,
    cargo_key=cargo['Net_CargoKey'],
    payment=cargo['Net_Payment']['BaseValue'],
    weight=cargo['Net_Weight'],
    damage=cargo['Net_Damage'],
    sender_point=sender,
    destination_point=destination,
    data=cargo,
  )

async def process_event(event, player):
  total_payment = 0
  subsidy = 0
  current_tz = timezone.get_current_timezone()
  timestamp = datetime.fromtimestamp(event['timestamp'] / 1000, tz=current_tz)

  match event['hook']:
    case "/Script/MotorTown.MotorTownPlayerController:ServerCargoArrived":
      logs = await asyncio.gather(*[
        process_cargo_log(cargo, player, timestamp)
        for cargo in event['data']['Cargos']
      ])
      await ServerCargoArrivedLog.objects.abulk_create(logs)
      subsidy = get_subsidy_for_cargos(logs)
      total_payment += sum([log.payment for log in logs]) + subsidy

    case "/Script/MotorTown.MotorTownPlayerController:ServerCargoDumped":
      cargo = event['data']['Cargo']
      log = await ServerCargoArrivedLog.objects.acreate(
        timestamp=timestamp,
        player=player,
        cargo_key=cargo['Net_CargoKey'],
        payment=cargo['Net_Payment']['BaseValue'],
        weight=cargo['Net_Weight'],
        damage=cargo['Net_Damage'],
        data=event['data'],
      )
      subsidy, _ = get_subsidy_for_cargo(log)
      total_payment += log.payment + subsidy

    case "/Script/MotorTown.MotorTownPlayerController:ServerSignContract":
      contract = event['data'].get('Contract')
      if contract:
        await ServerSignContractLog.objects.acreate(
          timestamp=timestamp,
          player=player,
          cargo_key=contract['Item'],
          amount=contract['Amount'],
          payment=contract['CompletionPayment']['BaseValue'],
          cost=contract['Cost']['BaseValue'],
        )

    case "/Script/MotorTown.MotorTownPlayerController:ServerContractCargoDelivered":
      contract = event['data'].get('Contract')
      if contract:
        log, _created = await ServerSignContractLog.objects.aget_or_create(
          guid=event['data']['ContractGuid'],
          defaults={
            'timestamp': timestamp,
            'player': player,
            'cargo_key': contract['Item'],
            'amount': contract['Amount'],
            'payment': contract['CompletionPayment']['BaseValue'],
            'cost': contract['Cost']['BaseValue'],
            'data': contract
          },
        )
      else:
        try:
          log = await ServerSignContractLog.objects.aget(
            guid=event['data']['ContractGuid'],
          )
        except ServerSignContractLog.DoesNotExist:
          return
      if event['data']['FinishedAmount'] == log.amount - 1:
        if not log.delivered:
          total_payment += log.payment
        log.delivered = True
      log.finished_amount = F('finished_amount') + 1
      await log.asave(update_fields=['finished_amount', 'delivered'])

    case "/Script/MotorTown.MotorTownPlayerController:ServerPassengerArrived":
      passenger = event['data']['Passenger']
      flag = passenger.get('Net_PassengerFlags', 0)

      base_payment = passenger['Net_Payment']
      log = ServerPassengerArrivedLog(
        timestamp=timestamp,
        player=player,
        passenger_type=passenger['Net_PassengerType'],
        distance=passenger['Net_Distance'],
        payment=base_payment,
        arrived=passenger['Net_bArrived'],
        comfort=bool(flag & 1),
        urgent=bool(flag & 2),
        limo=bool(flag & 4),
        offroad=bool(flag & 8),
        comfort_rating=passenger['Net_LCComfortSatisfaction'],
        urgent_rating=passenger['Net_TimeLimitPoint'],
        data=passenger,
      )
      if log.comfort:
        bonus_per_star = 0.2
        if log.limo:
          bonus_per_star = bonus_per_star * 1.3
        log.payment += base_payment * log.comfort_rating * bonus_per_star 
      if log.urgent:
        log.payment += base_payment * log.urgent_rating * 0.3
      await log.asave()

      subsidy = get_passenger_subsidy(log)
      total_payment += log.payment + subsidy

    case "/Script/MotorTown.MotorTownPlayerController:ServerTowRequestArrived":
      tow_request = event['data']['TowRequest']
      payment = tow_request['Net_Payment']
      await ServerTowRequestArrivedLog.objects.acreate(
        timestamp=timestamp,
        player=player,
        payment=payment,
        data=tow_request,
      )
      match tow_request.get('Net_TowRequestFlags', 0):
        case 1: # Flipped
          subsidy = 2_000 + payment * 1.0
        case _:
          subsidy = 2_000 + payment * 0.5
      total_payment += payment + subsidy


  return total_payment, subsidy

