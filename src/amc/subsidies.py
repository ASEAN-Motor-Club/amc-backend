import asyncio
from amc.mod_server import show_popup, transfer_money
from amc_finance.services import send_fund_to_player_wallet

cargo_names = {
  'Burger_01_Signature': 'Signature Burger',
  'Pizza_01_Premium': 'Premium Pizza',
  'GiftBox_01': 'Gift Box',
  'LiveFish_01': 'Live Fish',
  'Log_Oak_12ft': '12ft Oak Log',
}

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

