import asyncio
from datetime import datetime
from django.utils import timezone
from django.db.models import F
from amc.mod_server import get_webhook_events, show_popup
from amc.subsidies import (
  subsidise_delivery,
  repay_loan_for_profit,
  set_aside_player_savings,
  get_subsidy_for_cargo,
  get_subsidy_for_cargos,
  get_passenger_subsidy,
  subsidise_passenger,
  subsidise_player,
)
from amc.models import (
  Player,
  ServerCargoArrivedLog,
  ServerSignContractLog,
  ServerPassengerArrivedLog,
  ServerTowRequestArrivedLog,
)


async def on_player_profit(player, total_payment, session):
  loan_repayment = await repay_loan_for_profit(player, total_payment, session)
  await set_aside_player_savings(player, total_payment - loan_repayment, session)


async def monitor_webhook(ctx):
  http_client_mod = ctx.get('http_client_mod')
  events = await get_webhook_events(http_client_mod)

  current_tz = timezone.get_current_timezone()
  for event in events:
    timestamp = datetime.fromtimestamp(event['timestamp'] / 1000, tz=current_tz)
    try:
      match event['hook']:
        case "/Script/MotorTown.MotorTownPlayerController:ServerCargoArrived":
          player_id = event['data']['PlayerId']
          player = await Player.objects.aget(unique_id=player_id)
          logs = [
            ServerCargoArrivedLog(
              timestamp=timestamp,
              player=player,
              cargo_key=cargo['Net_CargoKey'],
              payment=cargo['Net_Payment']['BaseValue'],
              weight=cargo['Net_Weight'],
              damage=cargo['Net_Damage'],
              data=event['data'],
            )
            for cargo in event['data']['Cargos']
          ]
          await ServerCargoArrivedLog.objects.abulk_create(logs)
          asyncio.create_task(subsidise_delivery(logs, http_client_mod))
          total_subsidy = get_subsidy_for_cargos(logs)
          total_payment = sum([log.payment for log in logs]) + total_subsidy
          asyncio.create_task(
            on_player_profit(
              player,
              total_payment,
              http_client_mod
            )
          )

        case "/Script/MotorTown.MotorTownPlayerController:ServerCargoDumped":
          player_id = event['data']['PlayerId']
          player = await Player.objects.aget(unique_id=player_id)
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
          total_subsidy, _ = get_subsidy_for_cargo(log)
          total_payment = log.payment + total_subsidy
          asyncio.create_task(subsidise_delivery([log], http_client_mod))
          asyncio.create_task(
            on_player_profit(
              player,
              total_payment,
              http_client_mod
            )
          )


        case "/Script/MotorTown.MotorTownPlayerController:ServerSignContract":
          player_id = event['data']['PlayerId']
          player = await Player.objects.aget(unique_id=player_id)
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
          player_id = event['data']['PlayerId']
          player = await Player.objects.aget(unique_id=player_id)
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
                'data': event['data']
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
              asyncio.create_task(
                on_player_profit(player, log.payment, http_client_mod)
              )
            log.delivered = True
          log.finished_amount = F('finished_amount') + 1
          await log.asave(update_fields=['finished_amount', 'delivered'])

        case "/Script/MotorTown.MotorTownPlayerController:ServerPassengerArrived":
          player_id = event['data']['PlayerId']
          player = await Player.objects.aget(unique_id=player_id)
          passenger = event['data']['Passenger']
          log = await ServerPassengerArrivedLog.objects.acreate(
            timestamp=timestamp,
            player=player,
            passenger_type=passenger['Net_PassengerType'],
            distance=passenger['Net_Distance'],
            payment=passenger['Net_Payment'],
            arrived=passenger['Net_bArrived'],
            data=passenger,
          )
          subsidy = get_passenger_subsidy(log)
          if subsidy != 0:
            asyncio.create_task(subsidise_passenger(log, subsidy, player, http_client_mod))
          total_payment = log.payment + subsidy
          asyncio.create_task(
            on_player_profit(player, total_payment, http_client_mod)
          )

        case "/Script/MotorTown.MotorTownPlayerController:ServerTowRequestArrived":
          player_id = event['data']['PlayerId']
          player = await Player.objects.aget(unique_id=player_id)
          tow_request = event['data']['TowRequest']
          payment = tow_request['Net_Payment']
          await ServerTowRequestArrivedLog.objects.acreate(
            timestamp=timestamp,
            player=player,
            payment=payment,
            data=tow_request,
          )
          subsidy = 5_000
          if subsidy != 0:
            asyncio.create_task(subsidise_player(subsidy, player, http_client_mod))
          total_payment = payment + subsidy
          asyncio.create_task(
            on_player_profit(player, total_payment, http_client_mod)
          )

        case "/Script/MotorTown.MotorTownPlayerController:ServerSetMoney":
          player_id = event['data']['PlayerId']
          player = await Player.objects.aget(unique_id=player_id)
          character = await player.get_latest_character()
          character.money = event['data']['Money']
          await character.asave(update_fields=['money'])
    except Exception as e:
      if player_id := event['data'].get('PlayerId'):
        asyncio.create_task(
          show_popup(http_client_mod, f"Webhook failed, please send to discord:\n{e}", player_id=player_id)
        )
      raise e

