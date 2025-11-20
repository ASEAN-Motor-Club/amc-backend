from amc.enums import VehicleKeyByLabel, VEHICLE_DATA

async def show_popup(session, message, player_id=None, character_guid=None):
  params = {'message': message}
  if player_id is not None:
    params['playerId'] = str(player_id)
  if character_guid is not None:
    params['characterGuid'] = str(character_guid)
  await session.post("/messages/popup", json=params)

async def send_system_message(session, message, character_guid=None):
  params = {'message': message}
  params['characterGuid'] = str(character_guid)
  await session.post("/messages/system", json=params)

async def set_config(session, max_vehicles_per_player=12):
  params = {'MaxVehiclePerPlayer': max_vehicles_per_player}
  await session.post("/config", json=params)


async def set_character_name(session, character_guid, name):
  transfer = {
    'name': name,
  }
  async with session.put(f'/players/{character_guid}/name', json=transfer) as resp:
    if resp.status != 200:
      raise Exception('Failed to change name')

async def transfer_money(session, amount, message, player_id):
  transfer = {
    'Amount': amount,
    'Message': message,
  }
  async with session.post(f'/players/{player_id}/money', json=transfer) as resp:
    if resp.status != 200:
      raise Exception('Failed to transfer money')

async def toggle_rp_session(session, player_guid, despawn=False):
  data = {'despawn': despawn}
  async with session.post(f'/rp_sessions/{player_guid}/toggle', json=data) as resp:
    if resp.status != 200:
      raise Exception('Failed to toggle RP session')

async def join_player_to_event(session, event_guid, player_id):
  data = {
    'PlayerId': player_id,
  }
  async with session.post(f'/events/{event_guid}/join', json=data) as resp:
    if resp.status != 204:
      raise Exception('Failed to join event')

async def kick_player_from_event(session, event_guid, player_id):
  data = {
    'PlayerId': player_id,
  }
  async with session.post(f'/events/{event_guid}/leave', json=data) as resp:
    if resp.status != 204:
      raise Exception('Failed to kick player from event')


async def get_events(session):
  async with session.get('/events') as resp:
    if resp.status != 200:
      raise Exception('Failed to fetch events')
    data = await resp.json()
    return data['data']

async def list_player_vehicles(session, player_id, active=None, complete=None):
  params = {}
  if active:
    params['active'] = 1
  if complete:
    params['complete'] = 1
  async with session.get(f'/player_vehicles/{player_id}/list', params=params) as resp:
    if resp.status != 200:
      raise Exception(f'Failed to fetch player vehicles: {player_id}')
    data = await resp.json()
    player_vehicles = data['vehicles']
    if not player_vehicles:
      return {}
    res = {}
    if isinstance(player_vehicles, dict):
      for vehicle_id, vehicle in player_vehicles.items():
        res[vehicle_id] = {
          **vehicle
        }
        vehicle_name = vehicle['fullName'].split(' ')[0].replace('_C', '')
        res[vehicle_id]['VehicleName'] = vehicle_name
        asset_path = vehicle['classFullName'].split(' ')[1]
        res[vehicle_id]['AssetPath'] = asset_path

      return res
    elif isinstance(player_vehicles, list):
      for vehicle in player_vehicles:
        vehicle_id = vehicle['vehicleId']
        res[vehicle_id] = {
          **vehicle
        }
        vehicle_name = vehicle['fullName'].split(' ')[0].replace('_C', '')
        res[vehicle_id]['VehicleName'] = vehicle_name
        asset_path = vehicle['classFullName'].split(' ')[1]
        res[vehicle_id]['AssetPath'] = asset_path

      return res

async def send_message_as_player(session, message, player_id):
  data = {
    'Message': message,
  }
  async with session.post(f'/players/{player_id}/chat', json=data) as resp:
    if resp.status != 204:
      raise Exception('Failed to send message')

async def teleport_player(
  session,
  player_id,
  location,
  rotation=None,
  no_vehicles=False,
  reset_trailers=None,
  reset_carried_vehicles=None,
):
  data = {
    'Location': location,
  }
  if no_vehicles:
    data['NoVehicles'] = True
  if reset_trailers is not None:
    data['bResetTrailers'] = reset_trailers
  if reset_carried_vehicles is not None:
    data['bResetCarriedVehicles'] = reset_carried_vehicles
  if rotation:
    data['Rotation'] = rotation
  async with session.post(f'/players/{player_id}/teleport', json=data) as resp:
    if resp.status != 200:
      raise Exception('Failed to teleport player')

async def spawn_dealership(session, vehicle_key, location, yaw):
  data = {
    'Location': location,
    "Rotation": {
      "Roll": 0.0,
      "Pitch": 0.0,
      "Yaw": yaw
    },
    "VehicleClass": "",
    "VehicleParam": {
      "VehicleKey": vehicle_key,
    }
  }
  async with session.post('/dealers/spawn', json=data) as resp:
    if resp.status >= 400:
      raise Exception('Failed to spawn dealership')

async def get_player(session, player_id):
  async with session.get(f'/players/{player_id}') as resp:
    data = await resp.json()
    if not data or not data.get('data'):
      return None
    return data['data'][0]

async def get_players(session):
  async with session.get('/players') as resp:
    data = await resp.json()
    if not data or not data.get('data'):
      return None
    return data['data']

async def get_webhook_events(session):
  async with session.get('/webhook') as resp:
    data = await resp.json()
    return data

async def get_webhook_events2(session):
  async with session.get('/events') as resp:
    data = await resp.json()
    return data['events']

async def get_status(session):
  async with session.get('/status/general') as resp:
    data = await resp.json()
    if not data or not data.get('data'):
      return None
    return data['data']

async def get_rp_mode(session, player_id):
  async with session.get(f'/rp_sessions/{player_id}') as resp:
    if resp.status != 200:
      return False
    data = await resp.json()
    if not data or not data.get('isRpMode'):
      return False
    return data['isRpMode']

async def get_decal(session, player_id):
  async with session.get(f'/player_vehicles/{player_id}/decal') as resp:
    if resp.status != 200:
      raise Exception('Failed to get decal')
    data = await resp.json()
    return data

async def set_decal(session, player_id, decal):
  async with session.post(f'/player_vehicles/{player_id}/decal', json=decal) as resp:
    if resp.status != 200:
      raise Exception('Failed to set decal')

async def despawn_player_vehicle(session, player_id, category='current'):
  if category == 'others':
    json = {'others': True}
  elif category == 'all':
    json = {'all': True}
  else:
    json = {}
  async with session.post(f'/player_vehicles/{player_id}/despawn', json=json) as resp:
    if resp.status != 200:
      raise Exception('Failed to despawn')

async def force_exit_vehicle(session, character_guid):
  async with session.get(f'/player_vehicles/{character_guid}/exit') as resp:
    if resp.status != 200:
      raise Exception('Failed to exit vehicle')

async def set_world_vehicle_decal(
  session,
  vehicle_class,
  customization=None,
  decal=None,
  parts=None,
):
  data = {}
  if customization:
    data['customization'] = customization
  if decal:
    data['decal'] = decal
  if parts:
    data['parts'] = parts
  async with session.post(f'/world_vehicles/{vehicle_class}/decal', json=data) as resp:
    if resp.status != 200:
      raise Exception('Failed to set vehicle decals')


async def despawn_by_tag(
  session,
  tag
):
  data = {
    'Tag': tag,
    'Tags': []
  }
  async with session.post('/assets/despawn', json=data) as resp:
    if resp.status != 204:
      raise Exception('Failed to despawn by tag')

async def spawn_garage(
  session,
  location,
  rotation,
):
  data = {
    'Location': location,
    'Rotation': rotation
  }
  async with session.post('/garages/spawn', json=data) as resp:
    if resp.status != 201:
      raise Exception('Failed to spawn garage')
    data = await resp.json()
    return data['data']


async def spawn_vehicle(
  session,
  vehicle_label,
  location,
  rotation={},
  customization=None,
  decal=None,
  parts=None,
  driver_guid=None,
  tag="amc",
  extra_data={},
):
  try:
    vehicle_key = VehicleKeyByLabel.get(vehicle_label)
    if not vehicle_key:
      raise Exception(f'Vehicle {vehicle_label} not found')

    vehicle_data = VEHICLE_DATA.get(vehicle_key)
    if not vehicle_data:
      raise Exception(f'Vehicle data for key {vehicle_key} not found')
    asset_path = vehicle_data['asset_path']
  except Exception:
    asset_path = vehicle_label

  data = {
    'Location': location,
    'Rotation': rotation,
    'AssetPath': asset_path,
    'tag': tag,
    'driverGuid': driver_guid,
    **extra_data,
  }
  if customization:
    data['customization'] = customization
  if decal:
    data['decal'] = decal
  if parts:
    data['parts'] = parts

  async with session.post('/vehicles/spawn', json=data) as resp:
    if resp.status != 200:
      raise Exception('Failed to spawn vehicle')

