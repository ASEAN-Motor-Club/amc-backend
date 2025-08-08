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
  await session.post(f'/players/{player_id}/money', json=transfer)
