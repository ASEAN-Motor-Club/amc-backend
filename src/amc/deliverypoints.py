from amc.models import DeliveryPoint, DeliveryPointStorage
from amc.game_server import get_deliverypoints
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
          'amount': inventory['amount'],
        }
      )
    for inventory in dp.data['outputInventory']:
      await DeliveryPointStorage.objects.aupdate_or_create(
        delivery_point=dp,
        kind=DeliveryPointStorage.Kind.OUTPUT,
        cargo_key=inventory['cargoKey'],
        defaults={
          'amount': inventory['amount'],
        }
      )

