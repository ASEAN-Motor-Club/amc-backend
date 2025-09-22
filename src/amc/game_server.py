import asyncio
from yarl import URL
import urllib

async def game_api_request(session, url, method='get', password='', params={}):
  req_params = {'password': password, **params}
  params_str = urllib.parse.urlencode(req_params, quote_via=urllib.parse.quote)
  try:
    fn = getattr(session, method)
  except AttributeError as e:
    print(f"Invalid method: {e}")
    raise e

  async with fn(URL(f"{url}?{params_str}", encoded=True)) as resp:
    resp_json = await resp.json()
    return resp_json


async def get_players(session, password=''):
  data = await game_api_request(session, "/player/list")
  players = [
    (player['unique_id'], player['name'])
    for player in data['data'].values()
    if player is not None
  ]
  return players


async def announcement_request(message, session, password='', type="message", color=None):
  params = {'message': message}
  if type:
    params['type'] = type
  if color is not None:
    params['color'] = color
  return await game_api_request(session, "/chat", method='post', params=params)


async def announce(message: str, session, password='', clear_banner=True, type="message", color="FFFF00", delay=0):
  if delay > 0:
    await asyncio.sleep(delay)
  message_sanitized = message.strip().replace('\n', ' ')
  try:
    await announcement_request(message_sanitized, session, password, type=type, color=color)
    if type == "announce" and clear_banner:
      await announcement_request(' ', session, password)
  except Exception as e:
    print(f"Error sending message: {e}")
    raise e
