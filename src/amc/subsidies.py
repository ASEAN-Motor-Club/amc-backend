import asyncio
from decimal import Decimal
from amc.mod_server import show_popup, transfer_money
from amc.models import ServerPassengerArrivedLog
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
  repayment_percentage = Decimal(0.1) + (Decimal(0.4) * loan_utilisation)

  repayment = min(loan_balance, max(Decimal(1), int(payment * Decimal(repayment_percentage))))
  return repayment

async def repay_loan_for_profit(player, payment, session):
  try:
    character = await player.characters.with_last_login().filter(last_login__isnull=False).alatest('last_login')
    loan_balance = await get_player_loan_balance(character)
    if loan_balance == 0:
      return 0
    max_loan = get_character_max_loan(character)
    repayment = calculate_loan_repayment(Decimal(payment), loan_balance, max_loan)

    await transfer_money(
      session,
      int(-repayment),
      'ASEAN Loan Repayment',
      str(player.unique_id),
    )
    await register_player_repay_loan(repayment, character)
    return int(repayment)
  except Exception as e:
    asyncio.create_task(
      show_popup(session, f'Repayment failed {e}', player_id=player.unique_id)
    )
    raise e

DEFAULT_SAVING_RATE = 0
async def set_aside_player_savings(player, payment, session):
  try:
    character = await player.characters.with_last_login().filter(last_login__isnull=False).alatest('last_login')
    if character.saving_rate is not None:
      saving_rate = character.saving_rate
    else:
      saving_rate = min(Decimal(1), Decimal(DEFAULT_SAVING_RATE))
    if saving_rate == Decimal(0):
      return 0

    saving = Decimal(saving_rate) * Decimal(payment)
    if saving > 0:
      await transfer_money(
        session,
        int(-saving),
        'Earnings Deposit (Use /bank to see)',
        str(player.unique_id),
      )
      await register_player_deposit(saving, character, player)
      return int(saving)
  except Exception as e:
    asyncio.create_task(
      show_popup(session, f'Failed to deposit earnings:\n{e}', player_id=player.unique_id)
    )
    raise e


def get_subsidy_for_cargos(cargos):
  return sum([
    get_subsidy_for_cargo(cargo)[0]
    for cargo in cargos
  ])

def get_subsidy_for_cargo(cargo):
  subsidy_factor = 0.0
  match cargo.cargo_key:
    case 'GiftBox_01' | 'LiveFish_01':
      subsidy_factor = 3.0
    case 'Burger_01_Signature' | 'Pizza_01_Premium':
      if cargo.data.get('Net_TimeLeftSeconds', 0) > 0:
        subsidy_factor = 3.0
    case 'Log_Oak_12ft':
      subsidy_factor = 2.5 * (1.0 - cargo.damage)
    case _:
      subsidy_factor = 0.0
  return int(int(cargo.payment) * subsidy_factor), subsidy_factor

def get_passenger_subsidy(passenger):
  match passenger.passenger_type:
    case ServerPassengerArrivedLog.PassengerType.Taxi:
      return 2_000 + passenger.payment * 0.5
    case _:
      return 0

async def subsidise_player(subsidy, player, session):
  character = await player.characters.with_last_login().filter(last_login__isnull=False).alatest('last_login')
  message = 'ASEAN Subsidy' if subsidy > 0 else 'ASEAN Tax'
  await transfer_money(
    session,
    int(subsidy),
    message,
    player.unique_id,
  )
  await send_fund_to_player_wallet(subsidy, character, message)

