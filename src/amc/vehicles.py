import re
from amc.models import CharacterVehicle
from amc.mod_server import list_player_vehicles, spawn_vehicle, show_popup
from amc.enums import VehiclePartSlot

async def register_player_vehicles(http_client_mod, character, player):
  player_vehicles = await list_player_vehicles(http_client_mod, player.unique_id)
  if not player_vehicles:
    return

  for vehicle_id, vehicle in player_vehicles.items():
    if vehicle['companyGuid'] != ('0'*32):
      character = None

    config = {
      "CompanyGuid": vehicle['companyGuid'],
      "CompanyName": vehicle['companyName'],
      "Customization": vehicle['customization'],
      "Decal": vehicle['decal'],
      "Parts": vehicle['parts'],
      "Location": vehicle['position'],
      "Rotation": vehicle['rotation'],
    }
    vehicle_name = vehicle['fullName'].split(' ')[0].replace('_C', '')
    config['VehicleName'] = vehicle_name
    asset_path = vehicle['classFullName'].split(' ')[1]
    config['AssetPath'] = asset_path

    if character is not None:
      await CharacterVehicle.objects.aupdate_or_create(
        character=character,
        vehicle_id=int(vehicle_id),
        defaults={
          'config': config
        }
      )
    else:
      await CharacterVehicle.objects.aupdate_or_create(
        company_guid=vehicle['companyGuid'],
        vehicle_id=int(vehicle_id),
        defaults={
          'config': config
        }
      )

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

async def spawn_player_vehicle(http_client_mod, character, vehicle_id, location):
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

  await spawn_registered_vehicle(
    http_client_mod,
    vehicle,
    location=location,
    tag=character.name,
  )

async def spawn_registered_vehicle(
  http_client_mod,
  vehicle,
  location=None,
  rotation={},
  tag="player_vehicles"
):
  if not location:
    location = vehicle.config['Location']
  if not rotation:
    rotation = vehicle.config.get('Rotation', {})

  await spawn_vehicle(
    http_client_mod,
    vehicle.config['AssetPath'],
    location,
    rotation=rotation,
    customization=vehicle.config['Customization'],
    decal=vehicle.config['Decal'],
    parts=vehicle.config['Parts'],
    tag=tag,
  )

