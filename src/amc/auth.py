from django.core.signing import Signer

async def verify_player(player, signed_message):
  signer = Signer()
  discord_user_id = signer.unsign(signed_message)
  player.discord_user_id = int(discord_user_id)
  await player.asave(update_fields=['discord_user_id'])
  return int(discord_user_id)

