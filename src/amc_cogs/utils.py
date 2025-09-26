import discord
import re
from discord import app_commands
from amc.game_server import get_players
from amc.models import Character

def create_player_autocomplete(session, max_num=25):
  async def player_autocomplete(
    interaction: discord.Interaction,
    current: str
  ):
    players = await get_players(session)
    online_characters = (Character.objects
      .filter(
        name__icontains=current
      )
      .select_related('player')
      .filter(player__unique_id__in=[int(player_id) for player_id, player_name in players])
      .with_last_login()
      .order_by('name', '-last_login')
    )
    offline_characters = (Character.objects
      .filter(
        name__icontains=current
      )
      .select_related('player')
      .exclude(player__unique_id__in=[int(player_id) for player_id, player_name in players])
      .with_last_login()
      .order_by('name', '-last_login')
    )

    online_choices = [
      app_commands.Choice(name=f"{character.name} - {character.player.unique_id}", value=str(character.player.unique_id))
      async for character in online_characters[:max_num]
    ]
    offline_choices = []
    if len(online_choices) < max_num:
      offline_choices = [
        app_commands.Choice(name=f"{character.name} - {character.player.unique_id} (Offline)", value=str(character.player.unique_id))
        async for character in offline_characters[:(max_num - len(online_choices))]
      ]

    return [ *online_choices, *offline_choices ][:max_num]

  return player_autocomplete


def is_code_block_open(text):
    """Return True if there's an unclosed code block in the text."""
    return text.count("```") % 2 == 1

def split_markdown(text, max_length=4000):
    """
    Split markdown text into chunks of up to max_length characters,
    ensuring that code blocks (and similar formatting) are not broken.
    """
    # Split by paragraphs while preserving the delimiters (empty lines)
    parts = re.split(r'(\n\s*\n)', text)
    chunks = []
    current_chunk = ""

    for part in parts:
        # Check if adding this part would exceed the maximum allowed length
        if len(current_chunk) + len(part) > max_length:
            if is_code_block_open(current_chunk):
                # If we're in the middle of a code block, close it in the current chunk.
                current_chunk += "\n```"
                chunks.append(current_chunk)
                # Start the next chunk by reopening the code block.
                current_chunk = "```\n" + part
            else:
                chunks.append(current_chunk)
                current_chunk = part
        else:
            current_chunk += part

    # Append any remaining text, closing an unclosed code block if necessary.
    if current_chunk:
        if is_code_block_open(current_chunk):
            current_chunk += "\n```"
        chunks.append(current_chunk)

    return chunks



