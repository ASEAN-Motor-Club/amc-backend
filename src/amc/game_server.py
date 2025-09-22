import asyncio
from yarl import URL
import urllib


async def get_players(session, password=''):
    params = {'password': password}
    params_str = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    async with session.get(URL(f"/player/list?{params_str}", encoded=True)) as resp:
      data = await resp.json()
      players = [
        (player['unique_id'], player['name'])
        for player in data['data'].values()
        if player is not None
      ]
      return players


async def announcement_request(message, session, password='', type="message", color=None):
    params = {'password': password, 'message': message}
    if type:
      params['type'] = type
    if color is not None:
      params['color'] = color
    params_str = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return await session.post(URL(f"/chat?{params_str}", encoded=True))


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

