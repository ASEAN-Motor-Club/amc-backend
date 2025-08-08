from django.test import TestCase
from asgiref.sync import sync_to_async
from amc.factories import CharacterFactory, PlayerFactory
from .services import (
  get_player_bank_balance,
  register_player_deposit,
  register_player_withdrawal,
)

class BankAccountTestCase(TestCase):
  async def test_get_player_bank_balance(self):
    character = await sync_to_async(CharacterFactory)()
    balance = await get_player_bank_balance(character)
    self.assertEqual(balance, 0)

  async def test_register_player_deposit(self):
    character = await sync_to_async(CharacterFactory)()
    player = await sync_to_async(lambda: character.player)()
    await register_player_deposit(1000, character, player)
    balance = await get_player_bank_balance(character)
    self.assertEqual(balance, 1000)

  async def test_register_player_withdrawal(self):
    character = await sync_to_async(CharacterFactory)()
    player = await sync_to_async(lambda: character.player)()
    await register_player_deposit(1000, character, player)
    await register_player_withdrawal(100, character, player)
    balance = await get_player_bank_balance(character)
    self.assertEqual(balance, 900)

  async def test_register_player_withdrawal_more_than_balance(self):
    character = await sync_to_async(CharacterFactory)()
    player = await sync_to_async(lambda: character.player)()
    await register_player_deposit(100, character, player)
    with self.assertRaises(Exception):
      await register_player_withdrawal(1000, character, player)

