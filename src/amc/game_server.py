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
      ]
      return players


async def announcement_request(message, session, password=''):
    params = {'password': password, 'message': message}
    params_str = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return await session.post(URL(f"/chat?{params_str}", encoded=True))


async def announce(message: str, session, password='', clear_banner=True, delay=0):
    if delay > 0:
      await asyncio.sleep(delay)
    message_sanitized = message.strip().replace('\n', ' ')
    try:
        await announcement_request(message_sanitized, session, password)
        if clear_banner:
          await announcement_request(' ', session, password)
    except Exception as e:
        print(f"Error sending message: {e}")
        raise e

