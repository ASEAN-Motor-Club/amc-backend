import asyncio
from datetime import timedelta
from django.utils import timezone
from amc.models import (
  Character,
  CharacterLocation,
)
from amc.game_server import get_players2
from amc.mod_server import transfer_money
from amc_finance.services import send_fund_to_player_wallet

TASK_FREQUENCY = 20 # minutes
ACTIVE_GRANT_AMOUNT = 18_000 / (60 / TASK_FREQUENCY)
AFK_GRANT_AMOUNT = 6_000 / (60 / TASK_FREQUENCY)
MAX_LEVEL = 400

async def handout_ubi(ctx):
  http_client = ctx.get('http_client')
  http_client_mod = ctx.get('http_client_mod')
  now = timezone.now()

  players = await get_players2(http_client)
  for player_id, player in players:
    character = await Character.objects.aget(guid=player['character_guid'])
    if not character.driver_level or character.reject_ubi:
      continue

    is_online, is_active = await CharacterLocation.get_character_activity(
      character,
      now - timedelta(minutes=TASK_FREQUENCY),
      now
    )
    if is_active:
      grant_amount = ACTIVE_GRANT_AMOUNT
    else:
      grant_amount = AFK_GRANT_AMOUNT
    amount = min(grant_amount, character.driver_level * grant_amount * character.ubi_multiplier / MAX_LEVEL)

    await send_fund_to_player_wallet(amount, character, 'Universal Basic Income')
    await transfer_money(
      http_client_mod,
      int(amount),
      'Universal Basic Income',
      player_id
    )
    await asyncio.sleep(1)

