from amc.models import Company
from amc.game_server import get_world


async def monitor_corporations(ctx):
  session = ctx['http_client']

  world = await get_world(session)
  corps_data = world['character']['player']['companies']

  for corp_data in corps_data:
    try:
      corporation = await Company.objects.aget(
        name=corp_data['name'],
        owner__name=corp_data['ownerCharacterName'],
        owner__player__unique_id=corp_data['ownerCharacterId']['uniqueNetId'],
        is_corp=True,
      )
    except Company.DoesNotExist:
      print(f"Company {corp_data['name']} does not exist")
      continue
    except Company.MultipleObjectsReturned:
      print(f"There are multiple corporations named {corp_data['name']}")
      continue


    corporation.description = corp_data['shortDesc']
    corporation.money = corp_data['money']
    await corporation.asave()

