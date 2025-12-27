import asyncio
from amc.command_framework import registry, CommandContext
from amc.models import Delivery
from amc_finance.services import (
    register_player_withdrawal, register_player_take_loan,
    get_player_bank_balance, get_player_loan_balance,
    get_character_max_loan, calc_loan_fee,
    player_donation
)
from amc_finance.models import Account, LedgerEntry
from amc.subsidies import DEFAULT_SAVING_RATE
from amc.mod_server import transfer_money, show_popup
from amc.utils import with_verification_code
from decimal import Decimal
from django.utils.translation import gettext as _, gettext_lazy

@registry.register("/bank", description=gettext_lazy("Access your bank account"), category="Finance")
async def cmd_bank(ctx: CommandContext):
    balance = await get_player_bank_balance(ctx.character)
    loan_balance = await get_player_loan_balance(ctx.character)
    max_loan, reason = await get_character_max_loan(ctx.character)
    
    transactions = LedgerEntry.objects.filter(
        account__character=ctx.character,
        account__book=Account.Book.BANK,
    ).select_related('journal_entry').order_by('-journal_entry__created_at')[:10]
    
    transactions_str = '\n'.join([
        f"{tx.journal_entry.date} {tx.journal_entry.description:<25} <Money>{tx.credit - tx.debit:,}</>"
        async for tx in transactions
    ])
    
    saving_rate = ctx.character.saving_rate if ctx.character.saving_rate is not None else Decimal(DEFAULT_SAVING_RATE)
    
    await ctx.reply(_("""<Title>Your Bank ASEAN Account</>

<Bold>Balance:</> <Money>{balance:,}</>
<Small>Daily (IRL) Interest Rate: 2.2% (offline), 4.4% (online).</>
<Bold>Loans:</> <Money>{loan_balance:,}</>
<Bold>Max Available Loan:</> <Money>{max_loan:,}</>
<Small>{max_loan_reason}</>
<Bold>Earnings Saving Rate:</> <Money>{saving_rate_pct:.0f}%</>
<Small>Use /set_saving_rate [percentage] to automatically set aside your earnings into your account.</>

Commands:
<Highlight>/set_saving_rate [percentage]</> - Automatically set aside your earnings into your account
<Highlight>/withdraw [amount]</> - Withdraw from your bank account
<Highlight>/loan [amount]</> - Take out a loan

How to Put Money in the Bank
<Secondary>Use the /set_saving_rate command to set how much you want to save. It's 0 by default.</>
<Secondary>You can only fill your bank account by saving your earnings on this server, not through direct deposits.</>

How ASEAN Loans Works
<Secondary>Our loans have a flat one-off 10% fee, and you only have to repay them when you make a profit.</>
<Secondary>The repayment will range from 10% to 80% of your income, depending on the amount of loan you took.</>

<Bold>Latest Transactions</>
{transactions_str}
""").format(
        balance=balance,
        loan_balance=loan_balance,
        max_loan=max_loan,
        max_loan_reason=reason or _('Max available loan depends on your driver+trucking level'),
        saving_rate_pct=saving_rate * 100,
        transactions_str=transactions_str
    ))

@registry.register("/donate", description=gettext_lazy("Donate money to another player"), category="Finance")
async def cmd_donate(ctx: CommandContext, amount: str, verification_code: str = ""):
    amount_int = int(amount.replace(',', ''))
    code_expected, verified = with_verification_code((amount_int, ctx.character.id), verification_code)
    
    if not verified:
        await ctx.reply(_("<Title>Donation</>\nConfirm: <Highlight>/donate {amount} {code_expected}</>").format(
            amount=amount, code_expected=code_expected.upper()
        ))
        return

    await register_player_withdrawal(amount_int, ctx.character, ctx.player)
    await player_donation(amount_int, ctx.character)
    await ctx.reply(_("Donated {amount_int:,}!").format(amount_int=amount_int))

@registry.register("/withdraw", description=gettext_lazy("Withdraw money from your account"), category="Finance")
async def cmd_withdraw(ctx: CommandContext, amount: str, verification_code: str = ""):
    amount_int = int(amount.replace(',', ''))
    code_gen, verified = with_verification_code((amount_int, ctx.character.guid), verification_code)
    
    if amount_int > 1_000_000 and not verified:
        await ctx.reply(_("Confirm large withdrawal: /withdraw {amount} {code_gen}").format(
            amount=amount, code_gen=code_gen.upper()
        ))
        return
        
    await register_player_withdrawal(amount_int, ctx.character, ctx.player)
    await transfer_money(ctx.http_client_mod, int(amount_int), 'Bank Withdrawal', str(ctx.player.unique_id))

@registry.register("/loan", description=gettext_lazy("Take out a loan"), category="Finance")
async def cmd_loan(ctx: CommandContext, amount: str, verification_code: str = ""):
    if not (await Delivery.objects.filter(character=ctx.character).aexists()):
        await ctx.announce(_("You must have done at least one delivery"))
        return

    amount_int = int(amount.replace(',', ''))
    loan_balance = await get_player_loan_balance(ctx.character)
    max_loan, _ignored = await get_character_max_loan(ctx.character)
    amount_int = min(amount_int, max_loan - loan_balance)
    
    amount_int = min(amount_int, max_loan - loan_balance)
    
    code_expected, verified = with_verification_code((amount_int, ctx.character.id), verification_code)

    if not verified:
         fee = calc_loan_fee(amount_int, ctx.character, max_loan)
         await ctx.reply(_("<Title>Loan</>\nFee: {fee}\nConfirm: /loan {amount} {code_expected}").format(
             fee=fee, amount=amount, code_expected=code_expected.upper()
         ))
         return

    repay_amount, loan_fee = await register_player_take_loan(amount_int, ctx.character)
    await transfer_money(ctx.http_client_mod, int(amount_int), 'ASEAN Bank Loan', str(ctx.player.unique_id))
    await ctx.reply(_("Loan Approved!"))

@registry.register("/set_saving_rate", description=gettext_lazy("Set your automatic saving rate"), category="Finance")
async def cmd_set_saving_rate(ctx: CommandContext, saving_rate: str):
    try:
        rate = Decimal(saving_rate.replace('%', '')) / 100
        ctx.character.saving_rate = min(max(rate, 0), 1)
        await ctx.character.asave(update_fields=['saving_rate'])
        asyncio.create_task(show_popup(ctx.http_client_mod, _("<Title>Savings rate saved</>\n\n{rate:.0f}% of your earnings will automatically go into your bank account").format(
            rate=ctx.character.saving_rate * 100
        ), character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id)))
    except Exception as e:
        asyncio.create_task(show_popup(ctx.http_client_mod, _("<Title>Set savings rate failed</>\n\n{error}").format(error=e), character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id)))

@registry.register("/set_repayment_rate", description=gettext_lazy("Set your loan repayment rate"), category="Finance")
async def cmd_set_repayment_rate(ctx: CommandContext, repayment_rate: str):
    try:
        rate = Decimal(repayment_rate.replace('%', '')) / 100
        ctx.character.loan_repayment_rate = min(max(rate, 0), 1)
        await ctx.character.asave(update_fields=['loan_repayment_rate'])
        asyncio.create_task(show_popup(ctx.http_client_mod, _("<Title>Loan repayment rate saved</>\n\n{rate:.0f}% of your earnings will automatically go repaying loans, if any").format(
            rate=ctx.character.loan_repayment_rate * 100
        ), character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id)))
    except Exception as e:
        asyncio.create_task(show_popup(ctx.http_client_mod, _("<Title>Set loan repayment rate failed</>\n\n{error}").format(error=e), character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id)))

@registry.register("/toggle_ubi", description=gettext_lazy("Toggle Universal Basic Income"), category="Finance")
async def cmd_toggle_ubi(ctx: CommandContext):
    try:
        ctx.character.reject_ubi = not ctx.character.reject_ubi
        await ctx.character.asave(update_fields=['reject_ubi'])
        
        message = _("You will no longer receive a universal basic income") if ctx.character.reject_ubi else _("You will start to receive a universal basic income")
        
        asyncio.create_task(show_popup(ctx.http_client_mod, message, character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id)))
    except Exception as e:
        asyncio.create_task(show_popup(ctx.http_client_mod, _("<Title>Toggle UBI failed</>\n\n{error}").format(error=e), character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id)))

@registry.register("/burn", description=gettext_lazy("Burn money from your account"), category="Finance")
async def cmd_burn(ctx: CommandContext, amount: str, verification_code: str = ""):
    amount_int = int(amount.replace(',', ''))
    code_expected, verified = with_verification_code((amount_int, ctx.character.id), verification_code)
    
    if not verification_code:
        asyncio.create_task(
            show_popup(ctx.http_client_mod, _("""<Title>Burn</>

To prevent any mishap, please read the following:
- This action is non-reversible
- Please do not burn more than your wallet balance! You will end up with negative balance.

If you wish to proceed, type the command again followed by the verification code:
<Highlight>/burn {amount} {code_expected}</>""").format(
                amount=amount, code_expected=code_expected.upper()
            ), character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id))
        )
        return
    elif not verified:
        asyncio.create_task(
            show_popup(ctx.http_client_mod, _("""<Title>Burn</>

Sorry, the verification code did not match, please try again:
<Highlight>/burn {amount} {code_expected}</>""").format(
                amount=amount, code_expected=code_expected.upper()
            ), character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id))
        )
        return
    else:
        try:
            amount_int = max(0, amount_int)
            await transfer_money(ctx.http_client_mod, int(-amount_int), 'Burn', str(ctx.player.unique_id))
        except Exception as e:
            asyncio.create_task(
              show_popup(ctx.http_client_mod, _("<Title>Burn failed</>\n\n{error}").format(error=e), character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id))
            )

@registry.register("/repay_loan", description=gettext_lazy("Repay loan (Deprecated)"), category="Finance")
async def cmd_repay_loan(ctx: CommandContext, amount: str = ""):
    asyncio.create_task(
        show_popup(ctx.http_client_mod, _("<Title>Command Removed</>\n\nYou will automatically repay your loan as you earn money on the server"), character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id))
    )
