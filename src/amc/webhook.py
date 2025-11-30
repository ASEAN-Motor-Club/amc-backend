import json
import asyncio
import itertools
from operator import attrgetter
from datetime import datetime, timedelta
from django.utils import timezone
from django.contrib.gis.geos import Point
from django.db import transaction
from asgiref.sync import sync_to_async
from django.db.models import F, Q
from amc.game_server import announce
from amc.mod_server import get_webhook_events2, show_popup, get_rp_mode, despawn_player_vehicle
from amc.subsidies import (
  repay_loan_for_profit,
  set_aside_player_savings,
  get_subsidy_for_cargo,
  get_passenger_subsidy,
  subsidise_player,
)
from amc_finance.services import send_fund_to_player, get_treasury_fund_balance
from amc.models import (
  Player,
  ServerCargoArrivedLog,
  ServerSignContractLog,
  ServerPassengerArrivedLog,
  ServerTowRequestArrivedLog,
  Delivery,
  DeliveryPoint,
  DeliveryJob,
  Character,
  CharacterLocation,
)
from amc.locations import gwangjin_shortcut


async def on_player_profits(player_profits, session):
  for character, total_subsidy, total_payment in player_profits:
    await on_player_profit(character, total_subsidy, total_payment, session)

async def on_player_profit(character, total_subsidy, total_payment, session):
  if total_subsidy != 0:
    await subsidise_player(total_subsidy, character, session)
  loan_repayment = await repay_loan_for_profit(character, total_payment, session)
  savings = total_payment - loan_repayment
  if savings > 0:
    await set_aside_player_savings(character, savings, session)

async def on_delivery_job_fulfilled(job, http_client):
    """
    Finds all players who contributed to a job and rewards them proportionally.
    """

    if job.fulfilled_at is not None:
      return

    if job.quantity_fulfilled >= job.quantity_requested:
      job.fulfilled_at = timezone.now()
      await job.asave(update_fields=['fulfilled_at'])
    # Define a completion bonus. Defaults to 50,000 if not set on the job model.
    completion_bonus = getattr(job, 'completion_bonus', 50_000)
    if completion_bonus == 0:
        return

    log_qs = Delivery.objects.filter(job=job).order_by('timestamp')

    # Get the exact N logs that fulfilled the job by taking the most recent ones.
    contributing_logs = []
    acc = job.quantity_requested
    async for log in log_qs:
      log.quantity = min(log.quantity, acc)
      acc = acc - log.quantity
      contributing_logs.append(log)

    total_deliveries = job.quantity_fulfilled
    if not total_deliveries:
        return

    # Group logs by player to count each player's contribution.
    contributing_logs.sort(key=attrgetter('character_id'))
    character_contributions = {}
    for character_id, group in itertools.groupby(contributing_logs, key=attrgetter('character_id')):
        if character_id:
            character_deliveries = list(group)
            character_contributions[character_id] = {
              'count': sum([delivery.quantity for delivery in character_deliveries]),
              'reward': sum([
                int(delivery.quantity / total_deliveries * completion_bonus)
                for delivery in character_deliveries
              ]),
            }

    if not character_contributions:
        print('No character_contributions')
        return
    
    # Fetch all contributing Player objects in one query.
    character_ids = character_contributions.keys()
    characters = {c.id: c async for c in Character.objects.filter(id__in=character_ids)}

    # Distribute the bonus proportionally.
    contributors_names = []
    for character_id, character_contribution in character_contributions.items():
        character_obj = characters.get(character_id)
        if not character_obj:
            continue
        count = character_contribution['count']
        reward = character_contribution['reward']
        if reward > 0:
            await send_fund_to_player(reward, character_obj, "Job Completion")
            contributors_names.append(f"{character_obj.name} ({count})")

    contributors_str = ', '.join(contributors_names)
    message = f"\"{job.name}\" Completed! +${completion_bonus:,} has been deposited into your bank accounts. Thanks to: {contributors_str}"
    asyncio.create_task(announce(message, http_client, color="90EE90"))


async def post_discord_delivery_embed(
  discord_client,
  character,
  cargo_key,
  quantity,
  delivery_source,
  delivery_destination,
  payment,
  subsidy,
  vehicle_key,
  job=None,
):
  jobs_cog = discord_client.get_cog('JobsCog')
  delivery_source_name = ''
  delivery_destination_name = ''
  if delivery_source:
    delivery_source_name = delivery_source.name
  if delivery_destination:
    delivery_destination_name = delivery_destination.name

  if jobs_cog and hasattr(jobs_cog, 'post_delivery_embed'):
    loop = asyncio.get_running_loop()
    loop.run_in_executor(
      None,
      lambda: asyncio.run_coroutine_threadsafe(
        jobs_cog.post_delivery_embed(
          character.name,
          cargo_key,
          quantity,
          delivery_source_name,
          delivery_destination_name,
          payment,
          subsidy,
          vehicle_key,
          job=job,
        ),
        discord_client.loop
      )
    )

async def monitor_webhook(ctx):
  http_client = ctx.get('http_client')
  http_client_mod = ctx.get('http_client_mod')
  http_client_webhook = ctx.get('http_client_webhook')
  discord_client = ctx.get('discord_client')
  events = await get_webhook_events2(http_client_webhook)
  await process_events(events, http_client, http_client_mod, discord_client)

async def monitor_webhook_test(ctx):
  http_client = ctx.get('http_client_test')
  http_client_mod = ctx.get('http_client_test_mod')
  http_client_webhook = ctx.get('http_client_test_webhook')
  discord_client = ctx.get('discord_client')
  try:
    events = await get_webhook_events2(http_client_webhook)
  except Exception as e:
    print(f"Failed to get webhook events: {e}")
    return
  await process_events(events, http_client, http_client_mod, discord_client)


async def process_events(events, http_client=None, http_client_mod=None, discord_client=None):
  def key_fn(event):
    player_id = event['data'].get('CharacterGuid', '')
    if not player_id:
      player_id = event['data'].get('PlayerId', '')
    return (player_id, event['hook'])

  sorted_events = sorted(events, key=key_fn)
  grouped_events = itertools.groupby(sorted_events, key=key_fn)
  aggregated_events = []

  for key, group in grouped_events:
    if not key:
      continue

    group_events = list(group)
    match key[1]:
      case "ServerCargoArrived":
        cargos = [
          cargo
          for event in group_events
          for cargo in event['data']['Cargos']
        ]
        aggregated_events.append({
          'hook': key[1],
          'timestamp': group_events[0]['timestamp'],
          'data': {
            'CharacterGuid': key[0],
            'Cargos': cargos,
          }
        })
      case "ServerResetVehicleAtResponse":
        aggregated_events.append({
          'hook': key[1],
          'timestamp': group_events[0]['timestamp'],
          'data': {
            'CharacterGuid': key[0],
            'VehicleId': group_events[0]['data'].get('VehicleId'),
          }
        })
      case _:
        aggregated_events.extend(group_events)


  def key_by_character(event):
    player_id = event['data'].get('CharacterGuid', '')
    if not player_id:
      player_id = event['data'].get('PlayerId', '')
    return player_id

  sorted_player_events = sorted(aggregated_events, key=key_by_character)
  grouped_player_events = itertools.groupby(sorted_player_events, key=key_by_character)

  player_profits = []

  treasury_balance = await get_treasury_fund_balance()
  for character_guid, es in grouped_player_events:
    if not character_guid:
      continue

    try:
      character_q = Q(guid=character_guid, guid__isnull=False)
      try:
        character_q = character_q | Q(player__unique_id=int(character_guid))
      except ValueError:
        pass

      character = await (Character.objects
        .select_related('player')
        .with_last_login()
        .filter(character_q)
        .order_by('-last_login')
        .afirst()
      )
      player = character.player
    except Character.DoesNotExist:
      continue
    except Player.DoesNotExist:
      continue

    total_payment = 0
    total_subsidy = 0

    is_rp_mode = await get_rp_mode(http_client_mod, character_guid)
    used_shortcut = await CharacterLocation.objects.filter(
      character=character,
      location__coveredby=gwangjin_shortcut,
      timestamp__gte=timezone.now() - timedelta(hours=1)
    ).aexists()

    for event in es:
      try:
        payment, subsidy = await process_event(
          event,
          player,
          character,
          is_rp_mode,
          used_shortcut,
          treasury_balance,
          http_client,
          http_client_mod,
          discord_client
        )
        total_payment += payment
        total_subsidy += subsidy
      except Exception as e:
        event_str = json.dumps(event)
        asyncio.create_task(
          show_popup(http_client_mod, f"Webhook failed, please send to discord:\n{e}\n{event_str}", character_guid=character.guid)
        )
        raise e

    if used_shortcut:
      total_payment -= total_subsidy
      total_subsidy = 0

    player_profits.append((character, total_subsidy, total_payment))

  if http_client_mod:
    await on_player_profits(player_profits, http_client_mod)

async def process_cargo_log(cargo, player, character, timestamp):
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
    character=character,
    cargo_key=cargo['Net_CargoKey'],
    payment=cargo['Net_Payment'],
    weight=cargo.get('Net_Weight', 0),
    damage=cargo['Net_Damage'],
    sender_point=sender,
    destination_point=destination,
    data=cargo,
  )

def atomic_update_job(job_id, quantity):
  with transaction.atomic():
    job = DeliveryJob.objects.select_for_update().get(pk=job_id)
    quantity_to_add = 0
    if job:
      requested_remaining = job.quantity_requested - job.quantity_fulfilled
      quantity_to_add = min(requested_remaining, quantity)
      if quantity_to_add > 0:
        job.quantity_fulfilled = F('quantity_fulfilled') + quantity_to_add
        job.save(update_fields=['quantity_fulfilled'])
        job.refresh_from_db(fields=['quantity_fulfilled'])
    return job, quantity_to_add

async def process_event(event, player, character, is_rp_mode=False, used_shortcut=False, treasury_balance=None, http_client=None, http_client_mod=None, discord_client=None):
  print(event)
  total_payment = 0
  subsidy = 0
  current_tz = timezone.get_current_timezone()
  timestamp = datetime.fromtimestamp(event['timestamp'], tz=current_tz)

  vehicle_key = ""
  if character:
    latest_loc = await CharacterLocation.objects.filter(character=character).alatest('timestamp')
    vehicle_key = latest_loc.get_vehicle_key_display()

  match event['hook']:
    case "ServerCargoArrived":
      logs = await asyncio.gather(*[
        process_cargo_log(cargo, player, character, timestamp)
        for cargo in event['data']['Cargos']
      ])
      await ServerCargoArrivedLog.objects.abulk_create(logs)

      key_by_cargo = attrgetter('cargo_key')
      logs.sort(key=key_by_cargo)
      for cargo_key, group in itertools.groupby(logs, key=key_by_cargo):
        group_list = list(group)
        quantity = len(group_list)
        payment = group_list[0].payment
        delivery_source = group_list[0].sender_point
        delivery_destination = group_list[0].destination_point
        cargo_subsidy = get_subsidy_for_cargo(group_list[0], treasury_balance=treasury_balance)[0] * quantity
        cargo_name = group_list[0].get_cargo_key_display()

        job = await (DeliveryJob.objects
          .filter_active()
          .filter_by_delivery(delivery_source, delivery_destination, cargo_key)
        ).afirst()
        if job is not None and job.rp_mode and not is_rp_mode:
          job = None

        delivery_subsidy = cargo_subsidy
        if job and not used_shortcut:
          job, quantity_to_add = await sync_to_async(atomic_update_job)(job.id, quantity)
          bonus = quantity_to_add * job.bonus_multiplier * payment
          delivery_subsidy = max(cargo_subsidy, bonus)

        if is_rp_mode:
          delivery_subsidy = (subsidy * 1.5) + (payment * quantity * 0.5)

        await Delivery.objects.acreate(
          timestamp=timestamp,
          character=character,
          cargo_key=cargo_key,
          quantity=quantity,
          payment=payment * quantity,
          subsidy=delivery_subsidy,
          sender_point=delivery_source,
          destination_point=delivery_destination,
          job=job,
          rp_mode=is_rp_mode,
        )
        if job and job.quantity_fulfilled >= job.quantity_requested and not job.fulfilled_at:
          asyncio.create_task(on_delivery_job_fulfilled(job, http_client))

        # ADDED: Call the discord embed posting function
        if discord_client:
          asyncio.create_task(
            post_discord_delivery_embed(
              discord_client,
              character,
              cargo_name,
              quantity,
              delivery_source,
              delivery_destination,
              payment * quantity,
              delivery_subsidy,
              vehicle_key,
              job=job,
            )
          )

        subsidy += delivery_subsidy

      total_payment += sum([log.payment for log in logs]) + subsidy

    case "ServerCargoDumped":
      cargo = event['data']['Cargo']
      log = await ServerCargoArrivedLog.objects.acreate(
        timestamp=timestamp,
        player=player,
        cargo_key=cargo['Net_CargoKey'],
        payment=cargo['Net_Payment'],
        weight=cargo.get('Net_Weight', 0),
        damage=cargo['Net_Damage'],
        data=event['data'],
      )
      subsidy, _ = get_subsidy_for_cargo(log)
      total_payment += log.payment + subsidy

    case "ServerSignContract":
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

    case "ServerContractCargoDelivered":
      print(event)
      contract = event['data']
      if contract:
        log, _created = await ServerSignContractLog.objects.aget_or_create(
          guid=event['data']['ContractGuid'],
          defaults={
            'timestamp': timestamp,
            'player': player,
            'cargo_key': contract['Item'],
            'amount': contract['Amount'],
            'payment': contract['CompletionPayment'],
            'cost': contract.get('Cost', 0),
            'data': contract
          },
        )
      else:
        try:
          log = await ServerSignContractLog.objects.aget(
            guid=event['data'].get('ContractGuid'),
          )
        except ServerSignContractLog.DoesNotExist:
          return 0, 0
      log.finished_amount = F('finished_amount') + 1
      await log.asave(update_fields=['finished_amount'])
      await log.arefresh_from_db()
      if log.finished_amount == log.amount and not log.delivered:
        total_payment += log.payment
        log.delivered = True
        await log.asave(update_fields=['delivered'])

    case "ServerPassengerArrived":
      passenger = event['data']['Passenger']
      flag = passenger.get('Net_PassengerFlags', 0)

      base_payment = passenger['Net_Payment']
      log = ServerPassengerArrivedLog(
        timestamp=timestamp,
        player=player,
        passenger_type=passenger['Net_PassengerType'],
        distance=passenger.get('Net_Distance'),
        payment=base_payment,
        arrived=passenger.get('Net_bArrived', True),
        comfort=bool(flag & 1),
        urgent=bool(flag & 2),
        limo=bool(flag & 4),
        offroad=bool(flag & 8),
        comfort_rating=passenger.get('Net_LCComfortSatisfaction'),
        urgent_rating=passenger.get('Net_TimeLimitPoint'),
        data=passenger,
      )
      if log.passenger_type == ServerPassengerArrivedLog.PassengerType.Taxi:
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

    case "ServerTowRequestArrived":
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

    case "ServerResetVehicleAt":
      if is_rp_mode and character.last_login < timestamp - timedelta(seconds=15):
        await despawn_player_vehicle(http_client_mod, player.unique_id)
        asyncio.create_task(
          announce(
            f"{character.name}'s vehicle has been despawned for using roadside recovery while on RP mode",
            http_client,
            color="FFA500"
          )
        )


  return total_payment, subsidy

