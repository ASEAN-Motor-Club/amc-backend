import asyncio
from django.db.models import F
from amc.models import DeliveryPoint, DeliveryPointStorage
from amc.utils import lowercase_first_char_in_keys

# The API might not be stable. To be safe we throttle the requests 
BATCH_SIZE = 10

async def monitor_deliverypoints(ctx):
  http_session = ctx['http_client_mod']
  qs = DeliveryPoint.objects.order_by(F('last_updated').asc(nulls_first=True), '?')[:BATCH_SIZE]

  async for dp in qs:
    async with http_session.get(f'/delivery/points/{dp.guid}') as resp:
      if resp.status != 200:
        await dp.asave()
        continue
      dp_info = (await resp.json()).get('data', [])
      if not dp_info:
        await dp.asave()
        continue
      dp_info = dp_info[0]
      dp.data = {
        'inputInventory': lowercase_first_char_in_keys(
          dp_info['Net_InputInventory'].get('Entries', [])
        ),
        'outputInventory': lowercase_first_char_in_keys(
          dp_info['Net_OutputInventory'].get('Entries', [])
        ),
        'deliveries': lowercase_first_char_in_keys(
          dp_info.get('Net_Deliveries', [])
        ),
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
      await asyncio.sleep(1 / BATCH_SIZE)

