async def show_popup(session, message, player_id=None):
  params = {'message': message}
  if player_id is not None:
    params['playerId'] = str(player_id)
  await session.post("/messages/popup", json=params)


async def transfer_money(session, amount, message, player_id):
  transfer = {
    'Amount': amount,
    'Message': message,
  }
  async with session.post(f'/players/{player_id}/money', json=transfer) as resp:
    if resp.status != 200:
      raise Exception('Failed to transfer money')

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

async def send_message_as_player(session, message, player_id):
  data = {
    'Message': message,
  }
  async with session.post(f'/players/{player_id}/chat', json=data) as resp:
    if resp.status != 204:
      raise Exception('Failed to send message')

async def teleport_player(session, player_id, location, rotation=None, no_vehicles=False):
  data = {
    'Location': location,
  }
  if no_vehicles:
    data['NoVehicles'] = True
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

async def get_webhook_events(session):
  async with session.get('/webhook') as resp:
    data = await resp.json()
    return data

async def get_status(session):
  async with session.get('/status/general') as resp:
    data = await resp.json()
    if not data or not data.get('data'):
      return None
    return data['data']

