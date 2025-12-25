import asyncio
from decimal import Decimal
from django.db.models import Q
from django.contrib.gis.geos import Point
from amc.mod_server import show_popup, transfer_money
from amc.models import ServerPassengerArrivedLog, SubsidyRule, Cargo
from amc_finance.services import (
  send_fund_to_player_wallet,
  get_character_max_loan,
  get_player_loan_balance,
  register_player_repay_loan,
  register_player_deposit,
)

async def get_subsidies_text():
    text = "<Title>ASEAN Server Subsidies</>\n\n"
    
    rules = SubsidyRule.objects.filter(active=True).order_by('-priority')
    
    async for rule in rules:
        # Build Reward String
        if rule.reward_type == SubsidyRule.RewardType.PERCENTAGE:
            reward_str = f"{int(rule.reward_value * 100)}%"
        else:
            reward_str = f"{int(rule.reward_value)} coins"
            
        if rule.scales_with_damage:
             reward_str += " (Reduces with damage)"
        if rule.requires_on_time:
             reward_str += " (Must be on time)"

        # Build Cargo String
        cargos = [c async for c in rule.cargos.all()]
        if cargos:
            cargo_names_list = [c.label for c in cargos]
            cargo_str = ", ".join(cargo_names_list)
        else:
            cargo_str = "Any Cargo"
            
        text += f"<Bold>{cargo_str}</> - <Money>{reward_str}</>\n"
        
        # Secondary info (Areas & Points)
        source_areas = [a async for a in rule.source_areas.all()]
        source_points = [p async for p in rule.source_delivery_points.all()]
        all_sources = source_areas + source_points
        
        dest_areas = [a async for a in rule.destination_areas.all()]
        dest_points = [p async for p in rule.destination_delivery_points.all()]
        all_dests = dest_areas + dest_points
        
        if all_sources:
            source_names = ", ".join([obj.name for obj in all_sources])
            text += f"<Secondary>From: {source_names}</>\n"
            
        if all_dests:
            dest_names = ", ".join([obj.name for obj in all_dests])
            text += f"<Secondary>To: {dest_names}</>\n"
            
    return text

SUBSIDIES_TEXT = "Use await get_subsidies_text()"

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


async def get_subsidy_for_cargos(cargos, treasury_balance=None):
  total = 0
  for cargo in cargos:
      result = await get_subsidy_for_cargo(cargo, treasury_balance)
      total += result[0]
  return total

async def get_subsidy_for_cargo(cargo, treasury_balance=None):
  rules = SubsidyRule.objects.filter(active=True).order_by('-priority')

  # 1. Cargo Key Filter
  # Cargo type hierarchy checking is tricky in a single query if not explicitly linked.
  # For now, we assume simple key match or explicit Cargo object link.
  # cargo.cargo_key is a string. `SubsidyRule.cargos` is a ManyToMany to `Cargo`.
  # We should match rules where cargos IS NULL OR cargos__key = cargo.cargo_key
  rules = rules.filter(
      Q(cargos__isnull=True) | Q(cargos__key=cargo.cargo_key)
  )

  # 2. Source Area Filter
  if cargo.sender_point and cargo.sender_point.coord:
      # Match rules that have NO source requirement 
      # OR source area contains point
      # OR source delivery point is within 1m
      rules = rules.filter(
          Q(source_areas__isnull=True, source_delivery_points__isnull=True) |
          Q(source_areas__polygon__contains=cargo.sender_point.coord) |
          Q(source_delivery_points__coord__dwithin=(cargo.sender_point.coord, 1.0))
      )
  else:
      # If unknown source, only allow rules with NO source requirement
      rules = rules.filter(source_areas__isnull=True, source_delivery_points__isnull=True)

  # 3. Destination Area Filter
  # Special case: 'TrashBag' | 'Trash_Big' logic in old code used dynamic point distance. 
  # We assume new system uses predefined areas for Trash too.
  destination_coord = None
  if cargo.destination_point and cargo.destination_point.coord:
      destination_coord = cargo.destination_point.coord
  elif destination_location := cargo.data.get('Net_DestinationLocation'):
       # Handle dynamic destinations (e.g. Trash logic provided explicit coords in data)
       # Note: Coords in data are likely raw game coords. We need to respect SRID=3857.
       # Assuming input is consistent with DeliveryPoint (srid=3857) or needs casting?
       # Old code: Point(x,y,z) implied srid0 or default. Now we are strict.
       # Constructing point with srid=3857 is safe if we assume same coordinate system.
       destination_coord = Point(
          destination_location['X'],
          destination_location['Y'],
          destination_location['Z'],
          srid=3857
       )

  if destination_coord:
      rules = rules.filter(
          Q(destination_areas__isnull=True, destination_delivery_points__isnull=True) | 
          Q(destination_areas__polygon__contains=destination_coord) |
          Q(destination_delivery_points__coord__dwithin=(destination_coord, 1.0))
      )
  else:
      rules = rules.filter(destination_areas__isnull=True, destination_delivery_points__isnull=True)

  # 4. On Time Check
  is_on_time = cargo.data.get('Net_TimeLeftSeconds', 0) > 0
  if not is_on_time:
      rules = rules.exclude(requires_on_time=True)

  # Evaluate first match
  # Because of the complexity of the query (DISTINCT might be needed due to joins?), 
  # we iterate or take first.
  # Using .distinct() to avoid duplicate rule returned largely due to M2M joins.
  best_rule = await rules.distinct().afirst()

  subsidy_factor = 0.0
  subsidy_amount = 0

  if best_rule:
      factor = float(best_rule.reward_value)
      if best_rule.reward_type == SubsidyRule.RewardType.PERCENTAGE:
          subsidy_factor = factor
          # Scale with damage
          if best_rule.scales_with_damage:
               damage = cargo.damage if cargo.damage is not None else 0.0
               subsidy_factor *= (1.0 - damage)
          
          subsidy_amount = int(int(cargo.payment) * subsidy_factor)
      else:
          # Flat Amount
          subsidy_amount = int(factor)
          # Recalculate effective factor for display?
          if cargo.payment > 0:
            subsidy_factor = subsidy_amount / cargo.payment

      # Ministry Allocation Check
      # Skipping for now
      # TODO: Refactor, move this payment scaling responbility to the parent
      # remaining = best_rule.allocation - best_rule.spent
      # if remaining <= 0:
      #     subsidy_amount = 0
      #     subsidy_factor = 0
      # elif subsidy_amount > remaining:
      #     subsidy_amount = int(remaining)
      #     # Recalculate factor for reporting
      #     if cargo.payment > 0:
      #         subsidy_factor = subsidy_amount / cargo.payment

  # Treasury Cap Logic
  if treasury_balance is not None and subsidy_amount > 0:
    # Cap based on treasury health (legacy logic preserved)
    # "subsidy_factor = min(subsidy_factor, subsidy_factor * int(treasury_balance) / 50_000_000)"
    # This logic seems to scale down the subsidy if treasury is low?
    # If treasury < 50M, factor reduces linearly?
    # let's duplicate the logic exactly.
    
    current_factor = subsidy_factor
    scaling = int(treasury_balance) / 50_000_000
    effective_factor = min(current_factor, current_factor * scaling)
    
    # Recalculate amount based on effective factor?
    # Or just cap the amount? 
    # Old code: 
    # subsidy = min(int(int(cargo.payment) * subsidy_factor), int(treasury_balance))
    # Wait, old code line 208 updates subsidy_factor!
    
    subsidy_amount = int(int(cargo.payment) * effective_factor)
    subsidy_amount = min(subsidy_amount, int(treasury_balance))
    subsidy_factor = effective_factor

  return subsidy_amount, subsidy_factor, best_rule


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

