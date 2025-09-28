import asyncio
import random
from datetime import timedelta
from django.utils import timezone
from django.db.models import Q
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

  cargo_by_key = {cargo.key: cargo async for cargo in Cargo.objects.all()}

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
    for inventory in dp.data['inputInventory']:
      await DeliveryPointStorage.objects.aupdate_or_create(
        delivery_point=dp,
        kind=DeliveryPointStorage.Kind.INPUT,
        cargo_key=inventory['cargoKey'],
        defaults={
          'cargo': cargo_by_key.get(inventory['cargoKey']),
          'amount': inventory['amount'],
        }
      )
    for inventory in dp.data['outputInventory']:
      await DeliveryPointStorage.objects.aupdate_or_create(
        delivery_point=dp,
        kind=DeliveryPointStorage.Kind.OUTPUT,
        cargo_key=inventory['cargoKey'],
        defaults={
          'cargo': cargo_by_key.get(inventory['cargoKey']),
          'amount': inventory['amount'],
        }
      )

async def monitor_jobs(ctx):
  num_active_jobs = await DeliveryJob.objects.filter_active().acount()
  players = await get_players(ctx['http_client'])
  num_players = len(players)

  if num_active_jobs >= 5:
    return

  job_templates = (DeliveryJob.objects
    .filter(template=True)
    .prefetch_related('cargos', 'source_points', 'destination_points')
    .order_by('?')
  )
  async for job in job_templates:
    cargos = job.cargos.all()
    source_points = job.source_points.all()
    destination_points = job.destination_points.all()

    other_active_job_exists = await DeliveryJob.objects.filter_active().filter(
      Q(cargo_key=job.cargo_key) | Q(cargos__in=cargos),
      Q(source_points__in=source_points) | Q(destination_points__in=destination_points),
    ).aexists()
    job_recently_posted = await DeliveryJob.objects.filter(
      name=job.name,
      expired_at__gte=timezone.now() - timedelta(hours=6),
      template=False,
    ).aexists()
    if other_active_job_exists or job_recently_posted:
      continue

    destination_storages = DeliveryPointStorage.objects.filter(
      Q(cargo=job.cargo_key) | Q(cargo__in=cargos),
      delivery_point__in=destination_points,
    )
    source_storages = DeliveryPointStorage.objects.filter(
      Q(cargo=job.cargo_key) | Q(cargo__in=cargos),
      delivery_point__in=source_points,
    )

    if any([storage.capacity is None async for storage in destination_storages]):
      continue
    if any([storage.capacity is None async for storage in source_storages]):
      continue

    destination_storage_capacities = [
      (storage.amount, storage.capacity)
      async for storage in destination_storages
    ]
    source_storage_capacities = [
      (storage.amount, storage.capacity)
      async for storage in source_storages
    ]
    destination_amount = sum([amount for amount, capacity in destination_storage_capacities])
    destination_capacity = sum([capacity for amount, capacity in destination_storage_capacities])
    source_amount = sum([amount for amount, capacity in source_storage_capacities])
    source_capacity = sum([capacity for amount, capacity in source_storage_capacities])

    if destination_capacity == 0:
      is_destination_empty = True
    else:
      is_destination_empty = (destination_amount / destination_capacity) <= 0.15

    if source_capacity == 0:
      is_source_full = True
    else:
      is_source_full = (source_amount / source_capacity) >= 0.85

    if is_destination_empty and is_source_full:
      chance = job.job_posting_probability * max(10, num_players) / 200 / (5 + num_active_jobs)
      if not source_points and not destination_points:
        chance = chance / (24 * 3)

      if random.random() > chance:
        continue

      quantity_requested=min(
        job.quantity_requested,
        # source_amount,
        destination_capacity - destination_amount
      )
      new_job = await DeliveryJob.objects.acreate(
        name=job.name,
        cargo_key=job.cargo_key,
        quantity_requested=quantity_requested,
        expired_at=timezone.now() + timedelta(hours=5),
        bonus_multiplier=job.bonus_multiplier,
        completion_bonus=job.completion_bonus * quantity_requested / job.quantity_requested,
        description=job.description,
      )
      await new_job.cargos.aadd(*cargos)
      await new_job.source_points.aadd(*source_points)
      await new_job.destination_points.aadd(*destination_points)
      asyncio.create_task(
        announce(f"New job posting! {job.name}", ctx['http_client'])
      )

