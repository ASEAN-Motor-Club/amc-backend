import asyncio
from decimal import Decimal
from amc.mod_server import show_popup, transfer_money
from amc_finance.services import (
  send_fund_to_player_wallet,
  get_character_max_loan,
  get_player_loan_balance,
  register_player_repay_loan,
  register_player_deposit,
)

cargo_names = {
  'Burger_01_Signature': 'Signature Burger',
  'Pizza_01_Premium': 'Premium Pizza',
  'GiftBox_01': 'Gift Box',
  'LiveFish_01': 'Live Fish',
  'Log_Oak_12ft': '12ft Oak Log',
}

def calculate_loan_repayment(payment, loan_balance, max_loan):
  loan_utilisation = loan_balance / max_loan
  repayment_percentage = 0.1 + (0.4 * loan_utilisation)

  repayment = min(loan_balance, max(100, int(payment * Decimal(repayment_percentage))))
  return repayment

async def repay_loan_for_profit(player, payment, session):
  try:
    character = await player.characters.with_last_login().alatest('last_login')
    loan_balance = await get_player_loan_balance(character)
    if loan_balance == 0:
      return
    max_loan = get_character_max_loan(character)
    repayment = calculate_loan_repayment(payment, loan_balance, max_loan)

    await transfer_money(
      session,
      -repayment,
      'ASEAN Loan Repayment',
      player.unique_id,
    )
    await register_player_repay_loan(repayment, character)
  except Exception as e:
    asyncio.create_task(
      show_popup(session, f'Repayment failed {e}', player_id=player.unique_id)
    )

DEFAULT_SAVING_RATE = 0
async def set_aside_player_savings(player, payment, session):
  try:
    character = await player.characters.with_last_login().alatest('last_login')
    saving_rate = character.saving_rate if character.saving_rate is not None else Decimal(DEFAULT_SAVING_RATE)
    if saving_rate == Decimal(0):
      return

    saving = saving_rate * payment
    await transfer_money(
      session,
      -saving,
      'Earnings Deposit (Use /bank to see)',
      player.unique_id,
    )
    await register_player_deposit(saving, character)
  except Exception as e:
    asyncio.create_task(
      show_popup(session, f'Failed to deposit earnings:\n{e}', player_id=player.unique_id)
    )


async def subsidise_delivery(cargos, session):
  subsidy = 0
  popup_message = "<Title>ASEAN Subsidy Receipt</>"
  for cargo in cargos:
    subsidy_factor = 0.0
    match cargo.cargo_key:
      case 'Burger_01_Signature' | 'Pizza_01_Premium' | 'GiftBox_01' | 'LiveFish_01':
        if cargo.data['Net_TimeLeftSeconds'] > 0:
          subsidy_factor = 3.0
      case 'Log_Oak_12ft':
        subsidy_factor = 2.5 * (1.0 - cargo.damage)
      case _:
        pass
    if subsidy_factor != 0:
      cargo_subsidy = int(cargo.payment * subsidy_factor)
      subsidy += cargo_subsidy
      cargo_name = cargo_names.get(cargo.cargo_key, cargo.cargo_key)
      popup_message += f"\n{cargo_name} - <Money>{cargo_subsidy}</> ({int(subsidy_factor * 100):,}%)"

  if subsidy != 0:
    character = await cargo.player.characters.with_last_login().filter(last_login__isnull=False).alatest('last_login')
    await transfer_money(
      session,
      subsidy,
      'ASEAN Subsidy' if subsidy > 0 else 'ASEAN Tax',
      cargo.player.unique_id,
    )
    await send_fund_to_player_wallet(subsidy, character, "Delivery Subsidy")
    asyncio.create_task(
      show_popup(session, popup_message, player_id=cargo.player.unique_id)
    )

