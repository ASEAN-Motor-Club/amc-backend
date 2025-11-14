import asyncio
from decimal import Decimal
from django.contrib.gis.geos import Point
from amc.mod_server import show_popup, transfer_money
from amc.models import ServerPassengerArrivedLog
from amc_finance.services import (
  send_fund_to_player_wallet,
  get_character_max_loan,
  get_player_loan_balance,
  register_player_repay_loan,
  register_player_deposit,
)

SUBSIDIES_TEXT = """<Title>ASEAN Server Subsidies</>
<Warning>Using the Gwangjin shortcut will disqualify you from subsidies for 1 hour</>

<Bold>Depot Restocking</> <Money>10,000</> coins

<Bold>Burger, Pizza, Live Fish</> - <Money>300%</> (Must be on time)
<Bold>Airline Meal Pallets</> - <Money>200%</> (Must be on time)
<Bold>12ft Oak Log</> - <Money>250%</> (Reduces with damage)
<Bold>Coal & Iron Ore</> - <Money>150%</>
<Secondary>ONLY from Gwangjin Coal/Iron mines to Gwangjin Storages</>
<Bold>Planks</> - <Money>250%</>
<Secondary>ONLY from Gwangjin Plank Storage to Gwangjin Coal/Iron mines, Migeum Oak 1/2/3</>
<Bold>Fuel</> - <Money>150%</>
<Secondary>ONLY from Migeum Log Warehouse to Migeum Oak 1/2/3</>
<Secondary>ONLY from Gwangjin Fuel Storage to Gwangjin Coal/Iron mines</>
<Bold>Water Bottle Pallets</> - <Money>200 - 300%</>
<Secondary>To Gwangjin and Ara Supermarket - 300%</>
<Secondary>To other Supermarkets - 200%</>
<Bold>Meat Boxes</> - <Money>200%</> <Secondary>ONLY to Supermarkets</>
<Bold>Trash</> - <Money>100 - 250%</>
<Secondary>Gwangjin: 250% | Ara: 200% | Default: 100%</>

<Bold>To Gwangjin Supermarket</> - <Money>300%</>
<Secondary>Any cargo not already listed above</>

<Bold>Towing/Wrecker Jobs</>
Normal - <Money>2,000 + 50%</>
Flipped - <Money>2,000 + 100%</>

<Bold>Taxi</> - <Money>2,000 + 50%</>
"""
cargo_names = {

  'MeatBox': 'Meat Box',
  'BottlePallete': 'Water Bottle Pallete',
  'Burger_01_Signature': 'Signature Burger',
  'Pizza_01_Premium': 'Premium Pizza',
  'GiftBox_01': 'Gift Box',
  'LiveFish_01': 'Live Fish',
  'Log_Oak_12ft': '12ft Oak Log',
}

def calculate_loan_repayment(payment, loan_balance, max_loan, character_repayment_rate=None):
  loan_utilisation = loan_balance / max(max_loan, loan_balance)
  repayment_percentage = Decimal(0.5) + (Decimal(0.5) * loan_utilisation)
  if character_repayment_rate is not None:
    repayment_percentage = max(repayment_percentage, character_repayment_rate)

  repayment = min(loan_balance, max(Decimal(1), int(payment * Decimal(repayment_percentage))))
  return repayment

async def repay_loan_for_profit(character, payment, session):
  try:
    loan_balance = await get_player_loan_balance(character)
    if loan_balance == 0:
      return 0
    max_loan, _ = await get_character_max_loan(character)
    repayment = calculate_loan_repayment(
      Decimal(payment),
      loan_balance,
      max_loan,
      character_repayment_rate=character.loan_repayment_rate
    )

    await transfer_money(
      session,
      int(-repayment),
      'ASEAN Loan Repayment',
      str(character.player.unique_id),
    )
    await register_player_repay_loan(repayment, character)
    return int(repayment)
  except Exception as e:
    asyncio.create_task(
      show_popup(session, f'Repayment failed {e}', character_guid=character.guid)
    )
    raise e

DEFAULT_SAVING_RATE = 1
async def set_aside_player_savings(character, payment, session):
  try:
    if character.saving_rate is not None:
      saving_rate = character.saving_rate
    else:
      saving_rate = Decimal(DEFAULT_SAVING_RATE)
    if saving_rate == Decimal(0):
      return 0

    saving = Decimal(saving_rate) * Decimal(payment)
    if saving > 0:
      message = 'Earnings Bank Deposit'
      if character.saving_rate is None:
        message = 'Automated Bank Deposit (Use /bank to check your balance)'

      await transfer_money(
        session,
        int(-saving),
        message,
        str(character.player.unique_id),
      )
      await register_player_deposit(saving, character, character.player, "Earnings Deposit")
      return int(saving)
  except Exception as e:
    asyncio.create_task(
      show_popup(session, f'Failed to deposit earnings:\n{e}', character_guid=character.guid)
    )
    raise e


def get_subsidy_for_cargos(cargos):
  return sum([
    get_subsidy_for_cargo(cargo)[0]
    for cargo in cargos
  ])

def get_subsidy_for_cargo(cargo):
  subsidy_factor = 0.0
  sender_name = None
  destination_name = None
  if cargo.sender_point:
    sender_name = cargo.sender_point.name
  if cargo.destination_point:
    destination_name = cargo.destination_point.name

  match cargo.cargo_key:
    case 'Burger_01_Signature' | 'Pizza_01_Premium' | 'LiveFish_01':
      if cargo.data.get('Net_TimeLeftSeconds', 0) > 0:
        subsidy_factor = 3.0
    case 'AirlineMealPallet':
      if cargo.data.get('Net_TimeLeftSeconds', 0) > 0:
        subsidy_factor = 2.0
    case 'Log_Oak_12ft':
      subsidy_factor = 2.5 * (1.0 - cargo.damage)
    case 'Coal' | 'Iron Ore':
      match sender_name:
        case 'Gwangjin Coal' | 'Gwangjin Iron Ore Mine':
          match destination_name:
            case 'Gwangjin Iron Ore Storage' | 'Gwangjin Coal Storage':
              subsidy_factor = 1.5
    case 'WoodPlank_14ft_5t' | 'Fuel':
      match sender_name:
        case 'Gwangjin Plank Storage':
          match destination_name:
            case 'Gwangjin Iron Ore Mine' | 'Gwangjin Coal' | 'Migeum Oak 1' | 'Migeum Oak 2' | 'Migeum Oak 3':
              subsidy_factor = 2.5
        case 'Gwangjin Fuel Storage':
          match destination_name:
            case 'Gwangjin Iron Ore Mine' | 'Gwangjin Coal':
              subsidy_factor = 1.5
        case 'Migeum Log Warehouse':
          match destination_name:
            case 'Migeum Oak 1' | 'Migeum Oak 2' | 'Migeum Oak 3':
              subsidy_factor = 1.5
    case 'BottlePallete':
      match destination_name:
        case 'Gwangjin Supermarket' | 'Ara Supermarket':
          subsidy_factor = 3.0
        case _:
          if 'Supermarket' in destination_name:
            subsidy_factor = 2.0
          else:
            subsidy_factor = 0.0
    case 'MeatBox':
      if 'Supermarket' in destination_name:
        subsidy_factor = 2.0
      else:
        subsidy_factor = 0.0
    case 'TrashBag' | 'Trash_Big':
      subsidy_factor = 1.0
      if destination_location := cargo.data.get('Net_DestinationLocation'):
        destination_location = Point(
          destination_location['X'],
          destination_location['Y'],
          destination_location['Z'],
        )
        ara = Point(**{"x": 329486.94, "y": 1293697.78, "z": -18594.89})
        if destination_location.distance(ara) < 1_800_00:
          subsidy_factor = 2.0
        gwangjin = Point(318700.36, 816972.24, -1636.26)
        if destination_location.distance(gwangjin) < 2_000_00:
          subsidy_factor = 2.5
    case _:
      subsidy_factor = 0.0

  match destination_name:
    case 'Gwangjin Supermarket':
      if subsidy_factor == 0.0:
        subsidy_factor = 3.0
    case 'Gwangjin Supermarket Gas Station':
      if subsidy_factor == 0.0:
        subsidy_factor = 3.0

  return int(int(cargo.payment) * subsidy_factor), subsidy_factor

def get_passenger_subsidy(passenger):
  match passenger.passenger_type:
    case ServerPassengerArrivedLog.PassengerType.Taxi:
      return 2_000 + passenger.payment * 0.5
    case _:
      return 0

async def subsidise_player(subsidy, character, session, message=None):
  if message is None:
    message = 'ASEAN Subsidy' if subsidy > 0 else 'ASEAN Tax'
  await transfer_money(
    session,
    int(subsidy),
    message,
    character.player.unique_id,
  )
  await send_fund_to_player_wallet(subsidy, character, message)

