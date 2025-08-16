import discord
from discord import app_commands
from amc.game_server import get_players

def create_player_autocomplete(session):
  async def player_autocomplete(
    interaction: discord.Interaction,
    current: str
  ):
    players = await get_players(session)

    return [
      app_commands.Choice(name=f"{player_name} ({player_id})", value=player_id)
      for player_id, player_name in players
      if current.lower() in player_name.lower() or current in str(player_id)
    ][:25]  # Discord max choices: 25

  return player_autocomplete

