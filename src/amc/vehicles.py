from amc.models import CharacterVehicle
from amc.mod_server import list_player_vehicles

async def register_player_vehicles(http_client_mod, character, player):
  player_vehicles = await list_player_vehicles(http_client_mod, player.unique_id)

  for vehicle_id, vehicle in player_vehicles.items():
    config = {
      "CompanyGuid": vehicle['companyGuid'],
      "CompanyName": vehicle['companyName'],
      "Customization": vehicle['customization'],
      "Decal": vehicle['decal'],
      "Parts": vehicle['parts'],
    }
    vehicle_name = vehicle['fullName'].split(' ')[0].replace('_C', '')
    config['VehicleName'] = vehicle_name
    asset_path = vehicle['classFullName'].split(' ')[1]
    config['AssetPath'] = asset_path

    await CharacterVehicle.objects.aupdate_or_create(
      character=character,
      vehicle_id=vehicle_id,
      defaults={
        'config': config
      }
    )

