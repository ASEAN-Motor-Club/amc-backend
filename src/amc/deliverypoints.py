import math
import asyncio
import random
from datetime import timedelta
from django.utils import timezone
from django.db.models import Q, Prefetch
from amc.models import Cargo, DeliveryPoint, DeliveryPointStorage, DeliveryJob
from amc.game_server import get_deliverypoints, get_players, announce
from amc.enums import CargoKey

cargo_key_by_label = { v: k for k, v in CargoKey.choices }

def normalise_inventory(inventory):
  cargo = inventory['cargo']
  cargo_key = cargo_key_by_label.get(cargo['name'], cargo['name'])
  return {**inventory, 'cargoKey': cargo_key}

def normalise_delivery(delivery):
  cargo_key = cargo_key_by_label.get(delivery['cargo_type'], delivery['cargo_type'])
  return {**delivery, 'cargoKey': cargo_key}

async def monitor_deliverypoints(ctx):
  session = ctx['http_client']

  dps_info = await get_deliverypoints(session)
  dps_data = dps_info.get('data', {})

  cargo_by_key = {cargo.key: cargo async for cargo in Cargo.objects.select_related('type').all()}

  for dp_info in dps_data.values():
    try:
      dp = await DeliveryPoint.objects.aget(guid=dp_info['guid'].lower())
    except DeliveryPoint.DoesNotExist:
      print(f"Delivery point {dp_info['guid']} does not exist")
      continue

    dp.data = {
      'inputInventory': list(map(normalise_inventory, dp_info.get('InputInventory', {}).values())),
      'outputInventory': list(map(normalise_inventory, dp_info.get('OutputInventory', {}).values())),
      'deliveries': list(map(normalise_delivery, dp_info.get('Deliveries', {}).values())),
    }
    await dp.asave()

    storage_amounts = {}
    for inventory in dp.data['inputInventory']:
      cargo = cargo_by_key.get(inventory['cargoKey'])

      await DeliveryPointStorage.objects.aupdate_or_create(
        delivery_point=dp,
        kind=DeliveryPointStorage.Kind.INPUT,
        cargo_key=inventory['cargoKey'],
        defaults={
          'cargo': cargo,
          'amount': inventory['amount'],
        }
      )
      #if cargo.type:
      #  storage_amounts[cargo.type.key] = storage_amounts.get(cargo.type.key, 0) + inventory['amount']
      #  await DeliveryPointStorage.objects.aupdate_or_create(
      #    delivery_point=dp,
      #    kind=DeliveryPointStorage.Kind.INPUT,
      #    cargo_key=cargo.type.key,
      #    defaults={
      #      'cargo': cargo.type,
      #      'amount': storage_amounts[cargo.type.key],
      #    }
      #  )

    storage_amounts.clear()
    for inventory in dp.data['outputInventory']:
      cargo = cargo_by_key.get(inventory['cargoKey'])

      await DeliveryPointStorage.objects.aupdate_or_create(
        delivery_point=dp,
        kind=DeliveryPointStorage.Kind.OUTPUT,
        cargo_key=inventory['cargoKey'],
        defaults={
          'cargo': cargo_by_key.get(inventory['cargoKey']),
          'amount': inventory['amount'],
        }
      )
      #if cargo.type:
      #  storage_amounts[cargo.type.key] = storage_amounts.get(cargo.type.key, 0) + inventory['amount']
      #  await DeliveryPointStorage.objects.aupdate_or_create(
      #    delivery_point=dp,
      #    kind=DeliveryPointStorage.Kind.OUTPUT,
      #    cargo_key=cargo.type.key,
      #    defaults={
      #      'cargo': cargo.type,
      #      'amount': storage_amounts[cargo.type.key],
      #    }
      #  )

async def monitor_jobs(ctx):
  num_active_jobs = await DeliveryJob.objects.filter_active().acount()
  players = await get_players(ctx['http_client'])
  num_players = len(players)

  max_active_jobs = max(4, 2 + math.ceil(num_players / 6))

  if num_active_jobs >= max_active_jobs:
    return

  job_templates = (DeliveryJob.objects
    .filter(template=True)
    .exclude_has_conflicting_active_job()
    .exclude_recently_posted()
    .prefetch_related(
      Prefetch('cargos', queryset=Cargo.objects.select_related('type').all()),
      'source_points',
      'destination_points'
    )
    .order_by('?')
  )
  async for job in job_templates:
    cargos = job.cargos.all()
    source_points = job.source_points.all()
    destination_points = job.destination_points.all()

    non_type_cargos = [c for c in cargos if 'T::' not in c.key]
    destination_storages = DeliveryPointStorage.objects.filter(
      Q(cargo=job.cargo_key) | Q(cargo__in=non_type_cargos) | Q(cargo__type__in=cargos),
      delivery_point__in=destination_points,
    ).annotate_default_capacity()
    source_storages = DeliveryPointStorage.objects.filter(
      Q(cargo=job.cargo_key) | Q(cargo__in=non_type_cargos) | Q(cargo__type__in=cargos),
      delivery_point__in=source_points,
    ).annotate_default_capacity()

    destination_storage_capacities = [
      (storage.amount, storage.capacity_normalized or 0)
      async for storage in destination_storages
    ]
    source_storage_capacities = [
      (storage.amount, storage.capacity_normalized or 0)
      async for storage in source_storages
    ]
    destination_amount = sum([amount for amount, capacity in destination_storage_capacities])
    destination_capacity = sum([capacity for amount, capacity in destination_storage_capacities])
    source_amount = sum([amount for amount, capacity in source_storage_capacities])
    source_capacity = sum([capacity for amount, capacity in source_storage_capacities])

    quantity_requested = job.quantity_requested
    if job.expected_player_count_for_quantity:
      quantity_requested = min(
        quantity_requested,
        int(quantity_requested * num_players / job.expected_player_count_for_quantity)
      )

    if destination_capacity == 0:
      is_destination_empty = True
    else:
      is_destination_empty = (
        ((destination_amount / destination_capacity) <= 0.15)
        or
        (destination_capacity - destination_amount >= quantity_requested)
      )

    if destination_capacity > 0:
      quantity_requested = min(
        quantity_requested,
        destination_capacity - destination_amount
      )

    if source_capacity == 0:
      is_source_enough = True
    elif source_amount >= source_capacity * 0.85:
      is_source_enough = True
    else:
      is_source_enough = source_amount >= quantity_requested

    if not is_destination_empty or not is_source_enough:
      continue
    chance = job.job_posting_probability * max(10, num_players) / 2000 / (5 + num_active_jobs * 2)
    if not source_points and not destination_points:
      chance = chance / (24 * 3)

    if random.random() > chance:
      continue

    rp_mode =  job.rp_mode or random.random() < 0.15 
    bonus_multiplier = round(job.bonus_multiplier * random.uniform(0.8, 1.2), 2)
    completion_bonus = int(job.completion_bonus * quantity_requested / job.quantity_requested * random.uniform(0.7, 1.3))
    if rp_mode and not job.rp_mode:
      completion_bonus = completion_bonus * 2

    new_job = await DeliveryJob.objects.acreate(
      name=job.name,
      cargo_key=job.cargo_key,
      quantity_requested=quantity_requested,
      expired_at=timezone.now() + timedelta(hours=job.template_job_period_hours),
      bonus_multiplier=bonus_multiplier,
      completion_bonus=completion_bonus,
      description=job.description,
      rp_mode=rp_mode,
      base_template=job,
    )
    await new_job.cargos.aadd(*cargos)
    await new_job.source_points.aadd(*source_points)
    await new_job.destination_points.aadd(*destination_points)
    asyncio.create_task(
      announce(f"New job posting! {job.name} - {completion_bonus:,} bonus on completion.", ctx['http_client'])
    )
    break

