import asyncio
from yarl import URL
import urllib


async def announcement_request(message, session, password=''):
    params = {'password': password, 'message': message}
    params_str = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return await session.post(URL(f"/chat?{params_str}", encoded=True))


async def announce(message, session, password='', clear_banner=True, delay=0):
    if delay > 0:
      await asyncio.sleep(delay)
    try:
        await announcement_request(message, session, password)
        await announcement_request(' ', session, password)
    except Exception as e:
        print(f"Error sending message: {e}")
        raise e

