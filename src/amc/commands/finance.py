import asyncio
from amc.command_framework import registry, CommandContext
from amc.models import Delivery
from amc_finance.services import (
    register_player_withdrawal,
    player_donation,
    send_fund_to_player,
)
from amc_finance.loans import (
    register_player_take_loan,
    get_player_bank_balance,
    get_player_loan_balance,
    get_character_max_loan,
    get_character_npl_status,
    calc_loan_fee,
    get_credit_score_label,
)
from amc_finance.models import Account, LedgerEntry
from amc.subsidies import DEFAULT_SAVING_RATE
from amc.mod_server import transfer_money, show_popup
from amc.utils import with_verification_code
from decimal import Decimal
from django.utils.translation import gettext as _, gettext_lazy


@registry.register(
    "/bank",
    description=gettext_lazy("Access your bank account"),
    category="Finance",
    featured=True,
)
async def cmd_bank(ctx: CommandContext):
    balance = int(await get_player_bank_balance(ctx.character))
    loan_balance = int(await get_player_loan_balance(ctx.character))
    max_loan, reason = await get_character_max_loan(ctx.character)
    npl_status = await get_character_npl_status(ctx.character)

    transactions = (
        LedgerEntry.objects.filter(
            account__character=ctx.character,
            account__book=Account.Book.BANK,
        )
        .select_related("journal_entry")
        .order_by("-journal_entry__created_at")[:10]
    )

    transactions_str = "\n".join(
        [
            f"{tx.journal_entry.date} {tx.journal_entry.description:<25} <Money>{int(tx.credit - tx.debit):,}</>"
            async for tx in transactions
        ]
    )

    saving_rate = (
        ctx.character.saving_rate
        if ctx.character.saving_rate is not None
        else Decimal(DEFAULT_SAVING_RATE)
    )

    # Build NPL section if applicable
    npl_section = ""
    if npl_status:
        rate_pct = int(npl_status["repayment_rate"] * 100)
        if npl_status["is_npl"]:
            status_line = _(
                "<Warning>Your loan is behind on its payment plan. Make deliveries to catch up.</>"
            )
        else:
            status_line = _(
                "<EffectGood>You are meeting your payment plan requirements.</>"
            )
        npl_section = _(
            "\n<Bold>Payment Plan</>"
            "\n<Secondary>Period: {period_days} days | Required Repayment: {rate_pct}% of balance</>"
            "\n<Secondary>Repaid This Period:</> <Money>{repaid:,}</> / <Money>{required:,}</> <Secondary>required</>"
            "\n{status_line}"
        ).format(
            period_days=npl_status["period_days"],
            rate_pct=rate_pct,
            repaid=int(npl_status["total_repaid_in_period"]),
            required=int(npl_status["min_required_repayment"]),
            status_line=status_line,
        )

    await ctx.reply(
        _("""<Title>Your Bank ASEAN Account</>

<Bold>Balance:</> <Money>{balance:,}</>
<Small>Daily (IRL) Interest Rate: 2.2% (offline), 4.4% (online). Interest reduces for balances above $10M.</>
<Bold>Loans:</> <Money>{loan_balance:,}</>
<Bold>Max Available Loan:</> <Money>{max_loan:,}</>
<Small>{max_loan_reason}</>
<Bold>Credit Score:</> {credit_score} ({credit_label})
<Small>Your credit score affects loan fees. Timely repayments improve it.</>
<Bold>Earnings Saving Rate:</> <Money>{saving_rate_pct:.0f}%</>
<Small>Use /set_saving_rate [percentage] to automatically set aside your earnings into your account.</>{npl_section}

Commands:
<Highlight>/set_saving_rate [percentage]</> - Automatically set aside your earnings into your account
<Highlight>/withdraw [amount]</> - Withdraw from your bank account
<Highlight>/loan [amount]</> - Take out a loan

How to Put Money in the Bank
<Secondary>Use the /set_saving_rate command to set how much you want to save. It's 0 by default.</>
<Secondary>You can only fill your bank account by saving your earnings on this server, not through direct deposits.</>

How ASEAN Loans Works
<Secondary>Our loans have a flat one-off 10% fee, and you only have to repay them when you make a profit.</>
<Secondary>The repayment will range from 50% to 100% of your income, depending on the amount of loan you took.</>

<Bold>Latest Transactions</>
{transactions_str}
""").format(
            balance=balance,
            loan_balance=loan_balance,
            max_loan=max_loan,
            max_loan_reason=reason
            or _("Max available loan depends on your driver+trucking level"),
            saving_rate_pct=saving_rate * 100,
            credit_score=ctx.character.credit_score,
            credit_label=get_credit_score_label(ctx.character.credit_score),
            npl_section=npl_section,
            transactions_str=transactions_str,
        )
    )


@registry.register(
    "/donate",
    description=gettext_lazy("Donate money to the treasury"),
    category="Finance",
)
async def cmd_donate(ctx: CommandContext, amount: str, verification_code: str = ""):
    amount_int = int(amount.replace(",", ""))
    code_expected, verified = with_verification_code(
        (amount_int, ctx.character.id), verification_code
    )

    if not verified:
        await ctx.reply(
            _(
                "<Title>Donate to the Treasury</>\n"
                "Thank you for wanting to donate <Highlight>{amount:,}</>!\n"
                "To confirm, type: <Highlight>/donate {amount} {code_expected}</>\n"
                "<Secondary>This code is to make sure you don't donate by accident.</>"
            ).format(amount=amount_int, code_expected=code_expected.upper())
        )
        return

    await register_player_withdrawal(amount_int, ctx.character, ctx.player)
    await player_donation(amount_int, ctx.character)

    await ctx.character.arefresh_from_db()
    if ctx.discord_client:
        economy_cog = ctx.discord_client.get_cog("EconomyCog")
        if economy_cog:
            import asyncio

            asyncio.run_coroutine_threadsafe(
                economy_cog.send_donation_embed(ctx.character, amount_int),
                ctx.discord_client.loop,
            )

    await ctx.reply(_("Donated {amount_int:,}!").format(amount_int=amount_int))
    await ctx.announce(
        f"Thank you {ctx.character.name} for donating {amount_int:,} to the treasury!"
        f" (Total donations: {ctx.character.total_donations:,})"
    )


@registry.register(
    "/withdraw",
    description=gettext_lazy("Withdraw money from your account"),
    category="Finance",
)
async def cmd_withdraw(ctx: CommandContext, amount: str, verification_code: str = ""):
    amount_int = int(amount.replace(",", ""))
    code_gen, verified = with_verification_code(
        (amount_int, ctx.character.guid), verification_code
    )

    if amount_int >= 1_000_000 and not verified:
        await ctx.reply(
            _("Confirm large withdrawal: /withdraw {amount} {code_gen}").format(
                amount=amount, code_gen=code_gen.upper()
            )
        )
        return

    await register_player_withdrawal(amount_int, ctx.character, ctx.player)
    await transfer_money(
        ctx.http_client_mod,
        int(amount_int),
        "Bank Withdrawal",
        str(ctx.player.unique_id),
    )


@registry.register(
    "/loan", description=gettext_lazy("Take out a loan"), category="Finance"
)
async def cmd_loan(ctx: CommandContext, amount: str, verification_code: str = ""):
    if not (await Delivery.objects.filter(character=ctx.character).aexists()):
        await ctx.announce(_("You must have done at least one delivery"))
        return

    amount_int = int(amount.replace(",", ""))
    loan_balance = await get_player_loan_balance(ctx.character)
    max_loan, _ignored = await get_character_max_loan(ctx.character)
    amount_int = int(min(Decimal(amount_int), max_loan - loan_balance))

    code_expected, verified = with_verification_code(
        (amount_int, ctx.character.id), verification_code
    )

    if not verified:
        fee = calc_loan_fee(
            amount_int, ctx.character, max_loan, credit_score=ctx.character.credit_score
        )
        credit_label = get_credit_score_label(ctx.character.credit_score)
        # Calculate fee adjustment percentage vs neutral (score=100)
        base_fee = calc_loan_fee(amount_int, ctx.character, max_loan, credit_score=100)
        if base_fee > 0:
            fee_pct = int(((fee - base_fee) / base_fee) * 100)
            fee_adj = f" ({fee_pct:+d}% from credit)" if fee_pct != 0 else ""
        else:
            fee_adj = ""
        await ctx.reply(
            _(
                "<Title>Loan</>\nFee: {fee:,}{fee_adj}\nCredit Score: {credit_label}\nConfirm: /loan {amount} {code_expected}"
            ).format(
                fee=fee,
                fee_adj=fee_adj,
                credit_label=credit_label,
                amount=amount,
                code_expected=code_expected.upper(),
            )
        )
        return

    repay_amount, loan_fee = await register_player_take_loan(amount_int, ctx.character)
    await transfer_money(
        ctx.http_client_mod,
        int(amount_int),
        "ASEAN Bank Loan",
        str(ctx.player.unique_id),
    )
    await ctx.reply(_("Loan Approved!"))


@registry.register(
    "/set_saving_rate",
    description=gettext_lazy("Set your automatic saving rate"),
    category="Finance",
)
async def cmd_set_saving_rate(ctx: CommandContext, saving_rate: str):
    try:
        rate = Decimal(saving_rate.replace("%", "")) / 100
        ctx.character.saving_rate = min(max(rate, Decimal(0)), Decimal(1))
        await ctx.character.asave(update_fields=["saving_rate"])
        asyncio.create_task(
            show_popup(
                ctx.http_client_mod,
                _(
                    "<Title>Savings rate saved</>\n\n{rate:.0f}% of your earnings will automatically go into your bank account"
                ).format(rate=ctx.character.saving_rate * 100),
                character_guid=ctx.character.guid,
                player_id=str(ctx.player.unique_id),
            )
        )
    except Exception as e:
        asyncio.create_task(
            show_popup(
                ctx.http_client_mod,
                _("<Title>Set savings rate failed</>\n\n{error}").format(error=e),
                character_guid=ctx.character.guid,
                player_id=str(ctx.player.unique_id),
            )
        )


@registry.register(
    "/set_repayment_rate",
    description=gettext_lazy("Set your loan repayment rate"),
    category="Finance",
)
async def cmd_set_repayment_rate(ctx: CommandContext, repayment_rate: str):
    try:
        rate = Decimal(repayment_rate.replace("%", "")) / 100
        ctx.character.loan_repayment_rate = min(max(rate, Decimal(0)), Decimal(1))
        await ctx.character.asave(update_fields=["loan_repayment_rate"])
        asyncio.create_task(
            show_popup(
                ctx.http_client_mod,
                _(
                    "<Title>Loan repayment rate saved</>\n\n{rate:.0f}% of your earnings will automatically go repaying loans, if any"
                ).format(rate=ctx.character.loan_repayment_rate * 100),
                character_guid=ctx.character.guid,
                player_id=str(ctx.player.unique_id),
            )
        )
    except Exception as e:
        asyncio.create_task(
            show_popup(
                ctx.http_client_mod,
                _("<Title>Set loan repayment rate failed</>\n\n{error}").format(
                    error=e
                ),
                character_guid=ctx.character.guid,
                player_id=str(ctx.player.unique_id),
            )
        )


@registry.register(
    "/toggle_ubi",
    description=gettext_lazy("Toggle Universal Basic Income"),
    category="Finance",
)
async def cmd_toggle_ubi(ctx: CommandContext):
    try:
        ctx.character.reject_ubi = not ctx.character.reject_ubi
        await ctx.character.asave(update_fields=["reject_ubi"])

        message = (
            _("You will no longer receive UBI and subsidies")
            if ctx.character.reject_ubi
            else _("You will start to receive UBI and subsidies")
        )

        asyncio.create_task(
            show_popup(
                ctx.http_client_mod,
                message,
                character_guid=ctx.character.guid,
                player_id=str(ctx.player.unique_id),
            )
        )
    except Exception as e:
        asyncio.create_task(
            show_popup(
                ctx.http_client_mod,
                _("<Title>Toggle UBI failed</>\n\n{error}").format(error=e),
                character_guid=ctx.character.guid,
                player_id=str(ctx.player.unique_id),
            )
        )


@registry.register(
    "/burn",
    description=gettext_lazy("Burn money from your account"),
    category="Finance",
)
async def cmd_burn(ctx: CommandContext, amount: str, verification_code: str = ""):
    amount_int = int(amount.replace(",", ""))
    code_expected, verified = with_verification_code(
        (amount_int, ctx.character.id), verification_code
    )

    if not verification_code:
        asyncio.create_task(
            show_popup(
                ctx.http_client_mod,
                _("""<Title>Burn</>

To prevent any mishap, please read the following:
- This action is non-reversible
- Please do not burn more than your wallet balance! You will end up with negative balance.

If you wish to proceed, type the command again followed by the verification code:
<Highlight>/burn {amount} {code_expected}</>""").format(
                    amount=amount, code_expected=code_expected.upper()
                ),
                character_guid=ctx.character.guid,
                player_id=str(ctx.player.unique_id),
            )
        )
        return
    elif not verified:
        asyncio.create_task(
            show_popup(
                ctx.http_client_mod,
                _("""<Title>Burn</>

Sorry, the verification code did not match, please try again:
<Highlight>/burn {amount} {code_expected}</>""").format(
                    amount=amount, code_expected=code_expected.upper()
                ),
                character_guid=ctx.character.guid,
                player_id=str(ctx.player.unique_id),
            )
        )
        return
    else:
        try:
            amount_int = max(0, amount_int)
            await transfer_money(
                ctx.http_client_mod, int(-amount_int), "Burn", str(ctx.player.unique_id)
            )
        except Exception as e:
            asyncio.create_task(
                show_popup(
                    ctx.http_client_mod,
                    _("<Title>Burn failed</>\n\n{error}").format(error=e),
                    character_guid=ctx.character.guid,
                    player_id=str(ctx.player.unique_id),
                )
            )


@registry.register(
    "/repay_loan",
    description=gettext_lazy("Repay loan (Deprecated)"),
    category="Finance",
)
async def cmd_repay_loan(ctx: CommandContext, amount: str = ""):
    asyncio.create_task(
        show_popup(
            ctx.http_client_mod,
            _(
                "<Title>Command Removed</>\n\nYou will automatically repay your loan as you earn money on the server"
            ),
            character_guid=ctx.character.guid,
            player_id=str(ctx.player.unique_id),
        )
    )


async def _gov_ranking_text(character):
    """Build ranking and top-10 leaderboard text for gov employees."""
    from amc.models import Character

    rank = (
        await Character.objects.filter(
            gov_employee_contributions__gt=character.gov_employee_contributions,
        ).acount()
        + 1
    )
    total_ranked = await Character.objects.filter(
        gov_employee_contributions__gt=0,
    ).acount()

    top_10 = Character.objects.filter(
        gov_employee_contributions__gt=0,
    ).order_by("-gov_employee_contributions")[:10]
    leaderboard_lines = []
    i = 1
    async for c in top_10:
        leaderboard_lines.append(
            f"{i}. {c.name} \u2014 <Money>{c.gov_employee_contributions:,}</>"
        )
        i += 1
    leaderboard_str = "\n".join(leaderboard_lines)

    return (
        f"<Bold>Your Rank:</> #{rank} out of {total_ranked}\n\n"
        f"<Title>Top 10 Government Employees</>\n"
        f"{leaderboard_str}"
    )


@registry.register(
    "/workforgov",
    description=gettext_lazy("Opt in as a Government Employee for 24 hours"),
    category="Finance",
)
async def cmd_workforgov(ctx: CommandContext, verification_code: str = ""):
    from amc.gov_employee import activate_gov_role
    from amc.player_tags import refresh_player_name
    from django.utils import timezone

    character = ctx.character

    # If already active, show status and re-apply tag (self-healing)
    if character.is_gov_employee:
        remaining = character.gov_employee_until - timezone.now()
        hours = int(remaining.total_seconds() // 3600)
        minutes = int((remaining.total_seconds() % 3600) // 60)

        # Re-apply GOV tag in case it was lost (e.g. GUID resolution failed on login)
        if character.guid:
            await refresh_player_name(character, ctx.http_client_mod)

        ranking_text = await _gov_ranking_text(character)

        await ctx.reply(
            _(
                "<Title>Government Employee Status</>\n\n"
                "<Bold>Level:</> GOV{level}\n"
                "<Bold>Time Remaining:</> {hours}h {minutes}m\n"
                "<Bold>Total Contributions:</> <Money>{contributions:,}</>\n"
                "{ranking}"
            ).format(
                level=character.gov_employee_level,
                hours=hours,
                minutes=minutes,
                contributions=character.gov_employee_contributions,
                ranking=ranking_text,
            )
        )
        return

    code_expected, verified = with_verification_code(
        ("workforgov", ctx.character.id), verification_code
    )

    if not verified:
        ranking_text = ""
        if character.gov_employee_contributions > 0:
            ranking_text = "\n" + await _gov_ranking_text(character)

        await ctx.reply(
            _(
                "<Title>Work for Government</>\n"
                "You are about to become a <Highlight>Government Employee</> for 24 hours.\n\n"
                "<Warning>During this time:</>\n"
                "- ALL your income will go to the treasury\n"
                "- You will receive no subsidies or job bonuses\n"
                "- You will receive <EffectGood>2x UBI</>\n"
                "- Your contributions will count towards your GOV level\n\n"
                "Your current GOV level: <Highlight>GOV{level}</>\n"
                "To confirm, type: <Highlight>/workforgov {code}</>"
                "{ranking}"
            ).format(
                level=character.gov_employee_level or 1,
                code=code_expected.upper(),
                ranking=ranking_text,
            )
        )
        return

    await activate_gov_role(character, ctx.http_client_mod)
    await ctx.reply(
        _("You are now a Government Employee (GOV{level}) for 24 hours!").format(
            level=character.gov_employee_level
        )
    )
    await ctx.announce(
        f"{character.name} is now working as a Government Employee (GOV{character.gov_employee_level})!"
    )


@registry.register(
    "/claim_voucher",
    description=gettext_lazy("Claim a reward voucher by code"),
    category="Finance",
)
async def cmd_claim_voucher(ctx: CommandContext, code: str):
    from amc.models import Voucher
    from django.utils import timezone as tz

    code = code.upper().strip()
    try:
        voucher = await Voucher.objects.aget(code=code)
    except Voucher.DoesNotExist:
        await ctx.reply(
            _("<Title>Invalid Code</>\n\nNo voucher found with code: {code}").format(
                code=code
            )
        )
        return

    if voucher.is_claimed:
        await ctx.reply(
            _("<Title>Already Claimed</>\n\nThis voucher has already been claimed.")
        )
        return

    # If voucher is tied to a specific player, verify ownership
    if voucher.player_id is not None and voucher.player_id != ctx.player.pk:
        await ctx.reply(
            _("<Title>Not Your Voucher</>\n\nThis voucher belongs to another player.")
        )
        return

    # Deposit to bank account
    await send_fund_to_player(
        voucher.amount, ctx.character, f"Voucher: {voucher.reason}"
    )

    # Mark as claimed
    voucher.claimed_by = ctx.character
    voucher.claimed_at = tz.now()
    if voucher.player_id is None:
        voucher.player = ctx.player
    await voucher.asave(update_fields=["claimed_by", "claimed_at", "player"])

    await ctx.reply(
        _(
            "<Title>Voucher Claimed!</>\n\n"
            "<Bold>Amount:</> <Money>{amount:,}</>\n"
            "<Bold>Reason:</> {reason}\n"
            "Deposited to {character}'s bank account"
        ).format(
            amount=voucher.amount,
            reason=voucher.reason,
            character=ctx.character.name,
        )
    )
