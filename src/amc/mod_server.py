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


