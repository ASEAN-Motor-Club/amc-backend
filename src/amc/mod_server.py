async def show_popup(session, message, player_id=None):
  params = {'message': message}
  if player_id is not None:
    params['playerId'] = str(player_id)
  await session.post("/messages/popup", json=params)


