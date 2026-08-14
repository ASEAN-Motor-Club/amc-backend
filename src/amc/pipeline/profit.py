"""Profit processing pipeline.

Handles on_player_profit, on_player_profits, and split_party_payment.
Extracted from webhook.py.
"""

from __future__ import annotations

import logging
import os

from django.conf import settings

from amc.models import Character
from amc.mod_server import transfer_money
from amc.subsidies import set_aside_player_savings, subsidise_player
from amc_finance.loans import repay_loan_for_profit

logger = logging.getLogger("amc.webhook.profit")

PARTY_BONUS_ENABLED = os.environ.get("PARTY_BONUS_ENABLED", "").lower() in (
    "1",
    "true",
    "yes",
)
PARTY_BONUS_RATE = 0.05  # 5% per extra party member


async def on_player_profits(player_profits, session, http_client=None):
    for character, subsidy, base_payment, contract_payment in player_profits:
        await on_player_profit(
            character,
            subsidy,
            base_payment,
            session,
            http_client,
            contract_payment=contract_payment,
        )


async def on_player_profit(
    character,
    subsidy,
    base_payment,
    session,
    http_client=None,
    contract_payment=0,
    skip_gov_redirect=False,
):
    """Process a player's profit after party splitting.

    Args:
        character: The Character receiving the payment.
        subsidy: Subsidy portion (paid separately from wallet, not baked in).
        base_payment: What the game actually deposited into the wallet
            (excludes subsidy and contract).
        contract_payment: Contract completion payment deposited into wallet.
        session: HTTP client for mod server calls.
        http_client: HTTP client for API calls.
        skip_gov_redirect: If True, skip the gov employee income redirect
            (e.g. for UBI/salary which is already a government payment).
    """
    if character.reject_ubi:
        subsidy = 0

    if character.is_gov_employee and not skip_gov_redirect:
        from amc.gov_employee import redirect_income_to_treasury

        wallet_confiscation = base_payment + contract_payment
        if wallet_confiscation > 0:
            await transfer_money(
                session,
                int(-wallet_confiscation),
                "Government Service",
                str(character.player.unique_id),
            )
            if base_payment > 0:
                await redirect_income_to_treasury(
                    base_payment,
                    character,
                    "Government Service – Earnings",
                    http_client=http_client,
                    session=session,
                )
            if contract_payment > 0:
                await redirect_income_to_treasury(
                    contract_payment,
                    character,
                    "Government Service – Contract",
                    http_client=http_client,
                    session=session,
                )

        if subsidy > 0:
            await subsidise_player(subsidy, character, session)
            await transfer_money(
                session,
                int(-subsidy),
                "Government Service",
                str(character.player.unique_id),
            )
            await redirect_income_to_treasury(
                0,
                character,
                "Government Service – Subsidy",
                http_client=http_client,
                session=session,
                contribution=subsidy,
            )
        return

    if subsidy != 0:
        await subsidise_player(subsidy, character, session)
    actual_income = base_payment + subsidy + contract_payment

    if character.rp_mode and not skip_gov_redirect:
        rp_bonus = int(actual_income * settings.RP_MODE_BONUS_RATE)
        if rp_bonus > 0:
            await transfer_money(
                session,
                int(rp_bonus),
                "RP Mode Bonus",
                str(character.player.unique_id),
            )
            actual_income += rp_bonus

    loan_repayment = await repay_loan_for_profit(character, actual_income, session)
    savings = actual_income - loan_repayment
    if savings > 0:
        await set_aside_player_savings(character, savings, session)


async def split_party_payment(
    character,
    parties,
    total_base_payment,
    total_subsidy,
    total_contract_payment,
    http_client_mod,
    used_shortcut=False,
):
    """Split payment among party members, applying party bonus.

    Returns a list of (character, subsidy, base_payment, contract_payment) tuples
    for all party members, or None if the character is not in a multi-person party.

    The party bonus is calculated as a percentage of base_payment and added
    as extra subsidy. Wallet transfers move base_share + contract_share from
    the earner to each other member. Any integer division remainder stays with
    the earner.
    """
    if not PARTY_BONUS_ENABLED:
        return None
    if total_base_payment <= 0 and total_contract_payment <= 0:
        return None

    from amc.mod_server import get_party_members_for_character

    member_guids = get_party_members_for_character(parties, str(character.guid))
    party_size = len(member_guids)
    if party_size <= 1:
        return None

    # 1. Party bonus: percentage of base payment, added as subsidy only.
    party_multiplier = 1 + (party_size - 1) * PARTY_BONUS_RATE
    party_bonus = int(total_base_payment * (party_multiplier - 1))
    total_subsidy += party_bonus

    # 2. Equal split (remainder stays with earner)
    share_base = total_base_payment // party_size
    share_subsidy = total_subsidy // party_size
    share_contract = total_contract_payment // party_size

    # 3. Look up other party members
    other_guids = [g for g in member_guids if g.upper() != str(character.guid).upper()]
    other_characters = []
    if other_guids:
        other_characters = [
            c
            async for c in Character.objects.filter(
                guid__in=other_guids
            ).select_related("player")
        ]

    # 4. Wallet transfers
    wallet_share = share_base + share_contract
    others_withdrawal = wallet_share * len(other_characters)
    if others_withdrawal > 0 and http_client_mod:
        await transfer_money(
            http_client_mod,
            int(-others_withdrawal),
            "Party Split",
            str(character.player.unique_id),
        )

    for other_char in other_characters:
        if wallet_share > 0 and http_client_mod:
            await transfer_money(
                http_client_mod,
                int(wallet_share),
                "Party Share",
                str(other_char.player.unique_id),
            )

    # 5. Apply shortcut zone: zero out subsidy after bonus was factored in
    if used_shortcut:
        share_subsidy = 0

    # 6. Build profit tuples for all members
    earner_base = total_base_payment - share_base * len(other_characters)
    earner_contract = total_contract_payment - share_contract * len(other_characters)
    if used_shortcut:
        earner_subsidy = 0
    else:
        earner_subsidy = total_subsidy - share_subsidy * len(other_characters)

    result = [(character, earner_subsidy, earner_base, earner_contract)]
    for other_char in other_characters:
        result.append((other_char, share_subsidy, share_base, share_contract))
    return result
