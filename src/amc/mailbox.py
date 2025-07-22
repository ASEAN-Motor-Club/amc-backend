from django.utils import timezone
from amc.models import PlayerMailMessage
from amc.mod_server import show_popup

async def send_player_messages(http_client_mod, player):
  qs = PlayerMailMessage.objects.select_related('from_player').prefetch_related('from_player__characters').filter(
    to_player=player,
    received_at__isnull=True
  )
  async for m in qs:
    if m.from_player is not None:
      sender = str(m.from_player)
    else:
      sender = "The Admin Team"

    content = f"""\
You've got mail!
================

From: {sender}
Sent at: {m.sent_at}
Message:
{m.content}
"""
    await show_popup(http_client_mod, content, player_id=player.unique_id)
    m.received_at = timezone.now()
    await m.asave()

