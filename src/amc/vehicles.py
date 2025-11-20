import re
from amc.models import CharacterVehicle
from amc.mod_server import list_player_vehicles, spawn_vehicle, show_popup
from amc.enums import VehiclePartSlot

async def register_player_vehicles(http_client_mod, character, player, active=None):
  player_vehicles = await list_player_vehicles(http_client_mod, player.unique_id, active=active, complete=True)
  if not player_vehicles:
    return

  if not isinstance(player_vehicles, dict):
    return []

  results = []
  for vehicle_id, vehicle in player_vehicles.items():
    if int(vehicle_id) < 0:
      continue
    owner = character
    if len(vehicle['companyName']) > 0:
      owner = None

    config = {
      "CompanyGuid": vehicle['companyGuid'],
      "CompanyName": vehicle['companyName'],
      "Customization": vehicle['customization'],
      "Decal": vehicle['decal'],
      "Parts": vehicle['parts'],
      "Location": vehicle['position'],
      "Rotation": vehicle['rotation'],
      "Net_VehicleOwnerSetting": vehicle.get('Net_VehicleOwnerSetting', None),
    }
    vehicle_name = vehicle['fullName'].split(' ')[0].replace('_C', '')
    config['VehicleName'] = vehicle_name
    asset_path = vehicle['classFullName'].split(' ')[1]
    config['AssetPath'] = asset_path

    if owner:
      v, _ = await CharacterVehicle.objects.aupdate_or_create(
        character=owner,
        vehicle_id=int(vehicle_id),
        defaults={
          'config': config
        }
      )
    else:
      v, _ = await CharacterVehicle.objects.aupdate_or_create(
        company_guid=vehicle['companyGuid'],
        vehicle_id=int(vehicle_id),
        defaults={
          'config': config
        }
      )
    results.append(v)
  return results

def format_key_string(key_str):
    """
    Converts a key string into a more readable format.
    - Replaces underscores with spaces.
    - Splits CamelCase words by inserting a space before uppercase letters
      (unless it's the start of the string or already preceded by a space).

    Args:
        key_str (str): The input key string (e.g., "LSD_Clutch_2_100" or "HeavyMachineOffRoadFrontTire").

    Returns:
        str: The formatted, readable string.
    """
    if not key_str:
        return ""

    # 1. Replace all underscores with spaces
    s1 = key_str.replace('_', ' ')

    # 2. Use regex to insert a space before uppercase letters that follow a non-space character
    # (?<!^) ensures it's not the beginning of the string
    # (?<! ) ensures it's not already preceded by a space
    # ([A-Z]) captures the uppercase letter
    # r' \1' inserts a space before the captured letter
    s2 = re.sub(r'(?<!^)(?<! )([A-Z])', r' \1', s1)

    return s2

def format_vehicle_part_game(part):
  key = format_key_string(part['Key'])
  slot = VehiclePartSlot(part['Slot'])
  return f"{slot.name}: {key}"

def format_vehicle_part(part):
  key = format_key_string(part['Key'])
  slot = VehiclePartSlot(part['Slot'])
  return f"**{slot.name}**: {key}"

def format_vehicle_parts(parts):
  sorted_parts = sorted(parts, key=lambda p: p['Slot'])
  return '\n'.join([format_vehicle_part(p) for p in sorted_parts])

def format_vehicle_name(vehicle_full_name):
  vehicle_name = vehicle_full_name.split(' ')[0].replace('_C', '')
  return vehicle_name

async def spawn_player_vehicle(
  http_client_mod,
  character,
  vehicle_id,
  location,
  for_sale=False,
):
  try:
    vehicle = await CharacterVehicle.objects.aget(
      character=character,
      vehicle_id=vehicle_id,
    )
  except CharacterVehicle.DoesNotExist:
    await show_popup(
      http_client_mod,
      "Unrecognised vehicle ID. Please spawn it on the server at least once.",
      character_guid=character.guid
    )
    return

  if 'Raven' in vehicle.config['AssetPath'] or 'Terra' in vehicle.config['AssetPath'] or 'Formula' in vehicle.config['AssetPath']:
    raise Exception('You may not sell this vehicle')

  await spawn_registered_vehicle(
    http_client_mod,
    vehicle,
    location=location,
    tag=character.name,
    driver_guid=character.guid,
    for_sale=for_sale,
  )

async def spawn_registered_vehicle(
  http_client_mod,
  vehicle,
  location=None,
  rotation={},
  tag="player_vehicles",
  tags=[],
  driver_guid=None,
  for_sale=None,
  extra_data={},
):
  if not location:
    location = vehicle.config['Location']
  if not rotation:
    rotation = vehicle.config.get('Rotation', {})

  extra_data = {
    **extra_data,
    'profitShare': 0,
    'tags': tags,
  }
  if owner_setting := vehicle.config.get('Net_VehicleOwnerSetting'):
    extra_data['profitShare'] = owner_setting.get('VehicleOwnerProfitShare', 0)

  if vehicle.config.get('CompanyGuid') and vehicle.config.get('CompanyName'):
    extra_data['companyGuid'] = vehicle.config.get('CompanyGuid')
    extra_data['companyName'] = vehicle.config.get('CompanyName')
  if for_sale is not None:
    extra_data['forSale'] = for_sale
  else:
    extra_data['forSale'] = vehicle.for_sale


  await spawn_vehicle(
    http_client_mod,
    vehicle.config['AssetPath'],
    location,
    rotation=rotation,
    customization=vehicle.config['Customization'],
    decal=vehicle.config['Decal'],
    parts=[
      { **p, "partKey": p['Key'] }
      for p in vehicle.config['Parts']
    ],
    extra_data=extra_data,
    driver_guid=driver_guid,
    tag=tag,
  )

