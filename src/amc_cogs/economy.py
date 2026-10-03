import asyncio
import re
import logging
from io import BytesIO
from decimal import Decimal
from datetime import time as dt_time, timedelta, timezone as dt_timezone
from asgiref.sync import sync_to_async
from django.utils import timezone
from django.db import models
from django.db.models import (
    Sum,
    OuterRef,
    Subquery,
    Value,
    Q,
    F,
    DecimalField,
    Case,
    When,
    Count,
)
from django.db.models.functions import Coalesce
import discord
from discord import app_commands
from discord.ext import tasks, commands
from django.conf import settings
from amc.models import (
    Character,
    Player,
    ServerCargoArrivedLog,
    ServerSignContractLog,
    ServerPassengerArrivedLog,
    ServerTowRequestArrivedLog,
    Delivery,
)
from .utils import create_player_autocomplete, create_character_autocomplete
from amc.utils import get_timespan
from amc_finance.services import send_fund_to_player
from amc_finance.models import Account, BankPolicy, LedgerEntry
from amc_finance.services import (
    make_treasury_bank_deposit,
    make_treasury_bank_withdrawal,
    get_crossover_accounts,
    calculate_wealth_tax,
    calculate_hourly_interest,
)
from amc_finance.loans import (
    get_player_bank_balance,
    get_player_loan_balance,
    get_character_max_loan,
    get_non_performing_loans,
)
from amc_finance.treasury_summary import get_treasury_summary, save_treasury_snapshot
from amc.subsidies import DEFAULT_SAVING_RATE
from amc.save_file import decrypt, encrypt
from amc.gov_employee import GOV_BOARD_LIMIT, strip_gov_name
from amc_cogs.avatars import get_avatar_rgba, placeholder_rgba
from amc_cogs.leaderboard import _draw_panel


GOV_DAILY_FILE = "gov_daily_report.png"

DONATION_EXPECTATION_BRACKETS = [
    {"threshold": 3_000_000, "rate": Decimal("0.00")},
    {"threshold": 10_000_000, "rate": Decimal("0.10")},
    {"threshold": 50_000_000, "rate": Decimal("0.20")},
    {
        "threshold": None,
        "rate": Decimal("0.40"),
    },  # 'None' represents the highest bracket (100M+)
]


def get_progressive_donation_case(brackets_config):
    """
    Dynamically builds a Django ORM Case expression for progressive calculations
    based on a configuration list.
    """
    when_clauses = []
    cumulative_tax = Decimal(0)
    lower_bound = Decimal(0)
    default_expression = Value(Decimal(0))  # Default to 0 if config is empty

    for bracket in brackets_config:
        threshold = bracket.get("threshold")
        rate = bracket.get("rate")

        # The expression for calculating tax for earnings within this specific bracket
        then_expression = Value(cumulative_tax) + (
            F("total_earnings") - Value(lower_bound)
        ) * Value(rate)

        if threshold is None:
            # This is the final, highest bracket, which becomes the default case
            default_expression = then_expression
            break

        when_clauses.append(When(total_earnings__lt=threshold, then=then_expression))

        # Update cumulative values for the next iteration's calculation
        tax_in_this_bracket = (Decimal(str(threshold)) - lower_bound) * rate
        cumulative_tax += tax_in_this_bracket
        lower_bound = Decimal(str(threshold))

    return Case(*when_clauses, default=default_expression, output_field=DecimalField())



def _render_daily_gov_png(rows, date_label) -> BytesIO:
    """Render the daily gov report's top contributors as a dark PNG card.

    Same row-dict contract as the leaderboard card renderer: name, level,
    value (int), avatar_rgba (HxWx4 float array, or None for the neutral
    placeholder disc). Pure rendering — safe for asyncio.to_thread.
    """
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    bg = "#23272A"
    text = "#FFFFFF"
    dim = "#99AAB5"
    green = "#57F287"
    rank_colors = ["#FFD700", "#E5E7EB", "#E39774"]
    accent = "#00B0F4"

    placeholder = placeholder_rgba()
    for row in rows:
        if row["avatar_rgba"] is None:
            row["avatar_rgba"] = placeholder

    fig = Figure(figsize=(4.8, 4.8), dpi=150, facecolor=bg)
    FigureCanvasAgg(fig)
    ax = fig.add_axes([0.05, 0.07, 0.90, 0.86])
    _draw_panel(
        ax, f"TOP CIVIL SERVANTS — {date_label}", rows,
        accent, rank_colors, dim, text, green,
    )
    buf = BytesIO()
    fig.savefig(buf, format="png", facecolor=bg)
    buf.seek(0)
    return buf


def _set_daily_interest_rate(rate):
    """Persist the bank's daily interest rate on the BankPolicy singleton."""
    policy = BankPolicy.load()
    policy.daily_interest_rate = rate
    policy.save(update_fields=["daily_interest_rate"])
    return policy


def _set_gov_salary_multiplier(multiplier):
    """Persist the gov/police salary UBI multiplier on the BankPolicy singleton."""
    policy = BankPolicy.load()
    policy.gov_salary_multiplier = multiplier
    policy.save(update_fields=["gov_salary_multiplier"])
    return policy


def _set_wealth_tax_multiplier(multiplier):
    """Persist the wealth tax multiplier on the BankPolicy singleton."""
    policy = BankPolicy.load()
    policy.wealth_tax_multiplier = multiplier
    policy.save(update_fields=["wealth_tax_multiplier"])
    return policy


# Balances and offline durations used by the /set_wealth_tax preview tables.
WEALTH_TAX_PREVIEW_BALANCES = [5_000_000, 20_000_000, 50_000_000, 100_000_000, 250_000_000, 500_000_000]
WEALTH_TAX_PREVIEW_DAYS = [1, 7, 30, 90]


def _fmt_money(v: float) -> str:
    if abs(v) >= 100_000:
        return f"{v / 1_000_000:.2f}M"
    if abs(v) >= 10_000:
        return f"{v / 1_000:.1f}k"
    return f"{v:,.0f}"


def _wt_hourly_total(balance: int, hours_offline: float) -> float:
    """Marginal-bracket hourly wealth tax for a balance (pre-multiplier)."""
    return calculate_wealth_tax(balance, hours_offline)


def _int_hourly(balance: int, hours_offline: float, daily_rate: float) -> float:
    """Hourly interest for a balance at a given offline duration."""
    return calculate_hourly_interest(balance, hours_offline, daily_rate)


def _wt_interest_crossover_hours(
    balance: int, daily_rate: float, multiplier: float = 1.0
) -> float | None:
    """Smallest hours-offline where hourly wealth tax exceeds hourly interest.

    `multiplier` scales the tax side (the value being proposed). Multiplier 0
    short-circuits: tax never exceeds interest.
    None = interest wins at every simulated horizon (1h-1yr).
    """
    if multiplier <= 0:
        return None
    horizon = 365 * 24.0
    if _wt_hourly_total(balance, horizon) * multiplier <= _int_hourly(
        balance, horizon, daily_rate
    ):
        return None
    lo, hi = 1.0, horizon
    if _wt_hourly_total(balance, lo) * multiplier > _int_hourly(balance, lo, daily_rate):
        return lo
    for _ in range(60):
        mid = (lo + hi) / 2
        if _wt_hourly_total(balance, mid) * multiplier > _int_hourly(
            balance, mid, daily_rate
        ):
            hi = mid
        else:
            lo = mid
    return hi


def _wealth_tax_preview_pages(
    multiplier: Decimal, interest_rate: Decimal, current_multiplier: Decimal
) -> list[str]:
    """Build the simulated-outcome tables shown/confirmed by /set_wealth_tax.

    Returns 1-2 monospace code-block pages sized for Discord messages.
    `current_multiplier` is passed in by the caller (DB read stays outside).
    """
    mult = float(multiplier)
    daily_rate = float(interest_rate)

    lines = [
        (
            "**Simulated daily outcomes** (wealth tax at "
            f"{multiplier}x, interest at {interest_rate:.2%}/day)"
        ),
        "Cell = `wealth tax | interest` per day at that offline age:",
        "```",
        f"{'Balance':>10} | " + " | ".join(f"{d:>3}d" for d in WEALTH_TAX_PREVIEW_DAYS),
        "-|-".join(["-" * 10] + ["-" * 11] * len(WEALTH_TAX_PREVIEW_DAYS)),
    ]
    for balance in WEALTH_TAX_PREVIEW_BALANCES:
        cells = []
        for days in WEALTH_TAX_PREVIEW_DAYS:
            t = days * 24.0
            wt = _wt_hourly_total(balance, t) * mult * 24
            it = _int_hourly(balance, t, daily_rate) * 24
            cells.append(f"{_fmt_money(wt):>5}|{_fmt_money(it):>5}")
        lines.append(f"{balance / 1_000_000:>7.0f}M | " + " | ".join(cells))
    lines.append("```")
    page1 = "\n".join(lines)

    crossover_lines = [
        "**Offline time until wealth tax > interest** (at the new multiplier):",
        "```",
        f"{'Balance':>10} | crossover",
        "-".join(["-" * 10, "-" * 22]),
    ]
    for balance in WEALTH_TAX_PREVIEW_BALANCES:
        xh = _wt_interest_crossover_hours(balance, daily_rate, mult)
        if xh is None:
            label = "never (interest always wins)"
        else:
            label = f"{xh / 24:,.1f} days" if xh >= 24 else f"{xh:,.1f} hours"
        crossover_lines.append(f"{balance / 1_000_000:>7.0f}M | {label}")
    crossover_lines.append("```")
    crossover_lines.append(
        f"Current multiplier: **{current_multiplier}x** — "
        "confirm below to apply the new one."
    )
    page2 = "\n".join(crossover_lines)

    return [page1, page2]


class WealthTaxConfirmView(discord.ui.View):
    """Confirmation button for /set_wealth_tax. Only the invoker can confirm."""

    def __init__(self, cog, user_id: int, multiplier: Decimal, old_multiplier: Decimal):
        super().__init__(timeout=120)
        self.cog = cog
        self.user_id = user_id
        self.multiplier = multiplier
        self.old_multiplier = old_multiplier
        self.confirmed = False

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the command invoker can confirm.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button: discord.ui.Button):
        button.disabled = True
        await interaction.response.edit_message(view=self)
        if self.confirmed:
            return
        self.confirmed = True
        self.stop()

        await sync_to_async(_set_wealth_tax_multiplier)(self.multiplier)

        # Public (non-ephemeral) message with the same tables
        interest_rate = await sync_to_async(BankPolicy.get_daily_interest_rate)()
        pages = await sync_to_async(_wealth_tax_preview_pages)(
            self.multiplier,
            interest_rate,
            self.old_multiplier,
        )
        # One consolidated non-ephemeral reply in the command channel
        channel_text = "\n".join(
            [
                f"⚖️ Wealth tax multiplier set to **{self.multiplier}x** "
                f"(was {self.old_multiplier}x).",
                *pages,
            ]
        )
        await interaction.followup.send(content=channel_text)

        # One consolidated announcement in the treasury channel
        treasury_channel_id = getattr(
            settings, "DISCORD_TREASURY_CHANNEL_ID", 1402660537619320872
        )
        treasury_channel = self.cog.bot.get_channel(treasury_channel_id)
        if treasury_channel and treasury_channel.id != interaction.channel_id:
            treasury_text = "\n".join(
                [
                    f"⚖️ Wealth tax multiplier set to **{self.multiplier}x** "
                    f"(was {self.old_multiplier}x) by {interaction.user.mention}",
                    *pages,
                ]
            )
            await treasury_channel.send(treasury_text)
        elif not treasury_channel:
            logging.getLogger(__name__).warning(
                "Treasury channel %s not found; wealth tax tables not posted",
                treasury_channel_id,
            )
        else:
            logging.getLogger(__name__).warning(
                "Treasury channel %s not found; wealth tax tables not posted",
                treasury_channel_id,
            )

    async def on_timeout(self):
        self.stop()


class EconomyCog(commands.Cog):
    def __init__(self, bot, general_channel_id=settings.DISCORD_GENERAL_CHANNEL_ID):
        self.bot = bot
        self.general_channel_id = general_channel_id
        self.decrypt_save_file_channel_id = (
            settings.DISCORD_DECRYPT_SAVE_FILE_CHANNEL_ID
        )
        self.player_autocomplete = create_player_autocomplete(self.bot.http_client_game)
        self.character_autocomplete = create_character_autocomplete()
        self._avatar_cache: dict[int, tuple[float, bytes]] = {}

    async def cog_load(self):
        self.daily_top_haulers_task.start()
        self.weekly_donations_task.start()
        self.daily_gov_employee_summary_task.start()
        self.daily_treasury_summary_task.start()
        self.npl_warning_task.start()
        self.npl_collections_board_task.start()
        self.crossover_warning_task.start()

    @tasks.loop(time=dt_time(hour=2, minute=0, tzinfo=dt_timezone.utc))
    async def daily_top_haulers_task(self):
        embed = await self.build_top_haulers_embed()
        general_channel = self.bot.get_channel(self.general_channel_id)
        if general_channel:
            await general_channel.send(embed=embed)

    @tasks.loop(time=dt_time(hour=2, minute=0, tzinfo=dt_timezone.utc))
    async def daily_gov_employee_summary_task(self):
        embed, gov_file = await self.build_daily_gov_employee_embed()
        treasury_channel_id = getattr(
            settings, "DISCORD_TREASURY_CHANNEL_ID", 1402660537619320872
        )
        treasury_channel = self.bot.get_channel(treasury_channel_id)
        if treasury_channel:
            if gov_file is not None:
                sent_message = await treasury_channel.send(embed=embed, file=gov_file)
            else:
                sent_message = await treasury_channel.send(embed=embed)
            # Forward to #general
            general_channel = self.bot.get_channel(self.general_channel_id)
            if general_channel:
                await sent_message.forward(general_channel)

    @tasks.loop(time=dt_time(hour=2, minute=30, tzinfo=dt_timezone.utc))
    async def daily_treasury_summary_task(self):
        # Save snapshot for yesterday before posting (captures end-of-day balances)
        await sync_to_async(save_treasury_snapshot)()
        embed = await self.build_treasury_summary_embed()
        treasury_channel_id = getattr(
            settings, "DISCORD_TREASURY_CHANNEL_ID", 1402660537619320872
        )
        treasury_channel = self.bot.get_channel(treasury_channel_id)
        if treasury_channel:
            sent_message = await treasury_channel.send(embed=embed)
            general_channel = self.bot.get_channel(self.general_channel_id)
            if general_channel:
                await sent_message.forward(general_channel)

    async def build_daily_gov_employee_embed(self):
        now = timezone.now()
        yesterday = now - timedelta(days=1)

        # Filter for Government Service transactions (income + job bonus)
        gov_contributions = (
            LedgerEntry.objects.filter_donations()
            .filter(
                journal_entry__created_at__gte=yesterday,
                journal_entry__created_at__lte=now,
                journal_entry__description__startswith="Government Service",
            )
            .select_related("journal_entry", "journal_entry__creator")
            .values(
                "journal_entry__creator",
                "journal_entry__creator__player__discord_user_id",
            )
            .annotate(
                total=Sum("credit"),
                name=F("journal_entry__creator__name"),
                level=F("journal_entry__creator__gov_employee_level"),
            )
            .order_by("-total")
            .exclude(total=0)
        )

        contributors_list = []
        card_rows = []
        total_raised = Decimal(0)
        num_employees = 0

        async for row in gov_contributions:
            num_employees += 1
            total_raised += row["total"]
            level_str = (
                f"[GOV{row['level']}] " if row["level"] and row["level"] > 0 else ""
            )
            contributors_list.append(
                f"**{level_str}{row['name']}:** `{row['total']:,}`"
            )
            card_rows.append(row)

        contributors_str = (
            "\n".join(contributors_list)
            if contributors_list
            else "No government service income registered."
        )

        embed = discord.Embed(
            title="🏛️ Daily Government Employee Report",
            description=f"Generated for {yesterday.strftime('%A, %-d %B %Y')}",
            color=discord.Color.blue(),
            timestamp=now,
        )
        embed.add_field(
            name=f"Total Amount Treasury Raised: `{total_raised:,}`",
            value=f"From **{num_employees}** active civil servant{'s' if num_employees != 1 else ''} today.",
            inline=False,
        )
        embed.add_field(
            name="Top Contributors",
            value=contributors_str,
            inline=False,
        )

        # Avatar card for the top contributors (embed text can't carry
        # per-row images; same rendered-card approach as #leaderboard).
        gov_file = None
        if card_rows:
            avatar_rows = []
            for row in card_rows[:GOV_BOARD_LIMIT]:
                discord_id = row["journal_entry__creator__player__discord_user_id"]
                avatar_rows.append(
                    {
                        "name": strip_gov_name(row["name"] or "Unknown")[:14]
                        or "Unknown",
                        "level": row["level"],
                        "value": int(row["total"]),
                        "avatar_rgba": await get_avatar_rgba(
                            self.bot, discord_id, self._avatar_cache
                        ),
                    }
                )
            buf = await asyncio.to_thread(
                _render_daily_gov_png,
                avatar_rows,
                yesterday.strftime("%-d %b").upper(),
            )
            gov_file = discord.File(buf, filename=GOV_DAILY_FILE)
            embed.set_image(url=f"attachment://{GOV_DAILY_FILE}")
        return embed, gov_file

    @tasks.loop(time=dt_time(hour=8, minute=0, tzinfo=dt_timezone.utc))
    async def weekly_donations_task(self):
        if timezone.now().weekday() != 6:  # 6 = Sunday
            return
        embed = await self.build_weekly_donations_embed()
        treasury_channel_id = getattr(
            settings, "DISCORD_TREASURY_CHANNEL_ID", 1402660537619320872
        )
        treasury_channel = self.bot.get_channel(treasury_channel_id)
        if treasury_channel:
            sent_message = await treasury_channel.send(embed=embed)
            # Forward to #general
            general_channel = self.bot.get_channel(self.general_channel_id)
            if general_channel:
                await sent_message.forward(general_channel)

    async def build_weekly_donations_embed(self):
        now = timezone.now()
        this_week_start = now - timedelta(days=7)
        last_week_start = now - timedelta(days=14)
        last_week_end = this_week_start

        # --- This week's donors ---
        this_week_donors = (
            LedgerEntry.objects.filter_donations()
            .filter(
                journal_entry__created_at__gte=this_week_start,
                journal_entry__created_at__lte=now,
            )
            .select_related("journal_entry", "journal_entry__creator")
            .values("journal_entry__creator")
            .annotate(total=Sum("credit"), name=F("journal_entry__creator__name"))
            .order_by("-total")
        )
        donors_list = []
        this_week_total = Decimal(0)
        async for donor in this_week_donors:
            donors_list.append(f"**{donor['name']}:** {donor['total']:,}")
            this_week_total += donor["total"]
        donors_str = (
            "\n".join(donors_list) if donors_list else "No donations this week."
        )

        # --- Last week's total ---
        last_week_agg = await (
            LedgerEntry.objects.filter_donations()
            .filter(
                journal_entry__created_at__gte=last_week_start,
                journal_entry__created_at__lt=last_week_end,
            )
            .aaggregate(total=Sum("credit", default=Decimal(0)))
        )
        last_week_total = last_week_agg["total"]

        # --- Week-over-week comparison ---
        if last_week_total > 0:
            pct_change = ((this_week_total - last_week_total) / last_week_total) * 100
            if pct_change > 0:
                comparison_str = (
                    f"📈 **+{pct_change:.1f}%** vs last week (`{last_week_total:,}`)"
                )
            elif pct_change < 0:
                comparison_str = (
                    f"📉 **{pct_change:.1f}%** vs last week (`{last_week_total:,}`)"
                )
            else:
                comparison_str = f"➡️ **No change** vs last week (`{last_week_total:,}`)"
        elif this_week_total > 0:
            comparison_str = "🆕 No donations last week — great start!"
        else:
            comparison_str = "No donations in either week."

        # --- Treasury size ---
        treasury_fund, _ = await Account.objects.aget_or_create(
            account_type=Account.AccountType.ASSET,
            book=Account.Book.GOVERNMENT,
            character=None,
            name="Treasury Fund",
        )
        treasury_fund_in_bank, _ = await Account.objects.aget_or_create(
            account_type=Account.AccountType.ASSET,
            book=Account.Book.GOVERNMENT,
            character=None,
            name="Treasury Fund (in Bank)",
        )
        total_treasury = treasury_fund.balance + treasury_fund_in_bank.balance

        # --- Build embed ---
        embed = discord.Embed(
            title="📊 Weekly Donation Report",
            description=(
                f"Week of {this_week_start.strftime('%-d %b')} – {now.strftime('%-d %b %Y')}"
            ),
            color=discord.Color.gold(),
            timestamp=now,
        )
        embed.add_field(
            name="💰 Treasury",
            value=(
                f"**Vault:** `{treasury_fund.balance:,}`\n"
                f"**Bank Deposit:** `{treasury_fund_in_bank.balance:,}`\n"
                f"**Total:** `{total_treasury:,}`"
            ),
            inline=False,
        )
        embed.add_field(
            name=f"❤️ This Week's Donations — `{this_week_total:,}`",
            value=donors_str,
            inline=False,
        )
        embed.add_field(
            name="📈 Week-over-Week",
            value=comparison_str,
            inline=False,
        )
        return embed

    async def send_donation_embed(self, character, amount: int):
        channel_id = getattr(
            settings, "DISCORD_TREASURY_CHANNEL_ID", 1402660537619320872
        )
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

        embed = discord.Embed(
            title="New Donation! 💖",
            description=f"**{character.name}** has donated **${amount:,}** to the treasury!",
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="Total Overall Donations", value=f"${character.total_donations:,}"
        )

        await channel.send(embed=embed)

    async def player_autocomplete(self, interaction, current):
        return await self.player_autocomplete(interaction, current)

    async def character_autocomplete(self, interaction, current):
        return await self.character_autocomplete(interaction, current)

    @app_commands.command(name="calculate_gdp", description="Calculate the GDP figure")
    async def calculate_gdp(self, interaction, num_days: int = 1):
        await interaction.response.defer()
        start_time, end_time = get_timespan(num_days, num_days)

        subsidies_agg = await (
            LedgerEntry.objects.filter_subsidies()
            .filter(
                journal_entry__created_at__gte=start_time,
                journal_entry__created_at__lte=end_time,
            )
            .aaggregate(total_subsidies=Sum("debit", default=0))
        )
        deliveries_qs = ServerCargoArrivedLog.objects.filter(
            timestamp__gte=start_time, timestamp__lt=end_time
        )
        deliveries_aggregates = await deliveries_qs.aaggregate(
            total_payments=Sum("payment", default=0)
        )

        contracts_qs = ServerSignContractLog.objects.filter(
            timestamp__gte=start_time, timestamp__lt=end_time
        )
        contracts_aggregates = await contracts_qs.aaggregate(
            total_payments=Sum("payment", default=0)
        )

        passengers_qs = ServerPassengerArrivedLog.objects.filter(
            timestamp__gte=start_time, timestamp__lt=end_time
        )
        passengers_aggregates = await passengers_qs.aaggregate(
            total_payments=Sum("payment", default=0)
        )

        tow_requests_qs = ServerTowRequestArrivedLog.objects.filter(
            timestamp__gte=start_time, timestamp__lt=end_time
        )
        tow_requests_aggregates = await tow_requests_qs.aaggregate(
            total_payments=Sum("payment", default=0)
        )

        total_gdp = (
            subsidies_agg["total_subsidies"]
            + deliveries_aggregates["total_payments"]
            + contracts_aggregates["total_payments"]
            + passengers_aggregates["total_payments"]
            + tow_requests_aggregates["total_payments"]
        )

        delivery_sum_subquery = (
            ServerCargoArrivedLog.objects.filter(
                player=OuterRef("pk"), timestamp__gte=start_time, timestamp__lt=end_time
            )
            .values("player")
            .annotate(total=Sum("payment"))
            .values("total")
        )
        contracts_sum_subquery = (
            ServerSignContractLog.objects.filter(
                player=OuterRef("pk"), timestamp__gte=start_time, timestamp__lt=end_time
            )
            .values("player")
            .annotate(total=Sum("payment"))
            .values("total")
        )
        passengers_sum_subquery = (
            ServerPassengerArrivedLog.objects.filter(
                player=OuterRef("pk"), timestamp__gte=start_time, timestamp__lt=end_time
            )
            .values("player")
            .annotate(total=Sum("payment"))
            .values("total")
        )
        tow_requests_sum_subquery = (
            ServerTowRequestArrivedLog.objects.filter(
                player=OuterRef("pk"), timestamp__gte=start_time, timestamp__lt=end_time
            )
            .values("player")
            .annotate(total=Sum("payment"))
            .values("total")
        )

        top_players_qs = (
            Player.objects.annotate(
                gdp_contribution=Coalesce(
                    Subquery(delivery_sum_subquery, output_field=models.IntegerField()),
                    Value(0),
                )
                + Coalesce(
                    Subquery(
                        contracts_sum_subquery, output_field=models.IntegerField()
                    ),
                    Value(0),
                )
                + Coalesce(
                    Subquery(
                        passengers_sum_subquery, output_field=models.IntegerField()
                    ),
                    Value(0),
                )
                + Coalesce(
                    Subquery(
                        tow_requests_sum_subquery, output_field=models.IntegerField()
                    ),
                    Value(0),
                )
            )
            .filter(gdp_contribution__gt=0)
            .order_by("-gdp_contribution")[:20]
        )

        async def get_player_name(player):
            if player.discord_user_id:
                try:
                    user = await interaction.guild.fetch_member(player.discord_user_id)
                    return user.display_name
                except discord.NotFound:
                    pass
            try:
                latest_character = await (
                    Character.objects.with_last_login()
                    .filter(player=player, last_login__isnull=False)
                    .alatest("last_login")
                )
            except Character.DoesNotExist:
                return player.unique_id
            except Exception:
                return f"Character not found ({player.unique_id})"
            return latest_character.name or latest_character.id

        top_players_str = "\n".join(
            [
                f"**{await get_player_name(player)}:** {player.gdp_contribution:,}"
                async for player in top_players_qs
            ]
        )
        await interaction.followup.send(f"""
# Total GDP: {total_gdp:,}
-# {start_time} - {end_time}

Subsidies: {subsidies_agg["total_subsidies"]:,}
Deliveries: {deliveries_aggregates["total_payments"]:,}
Contracts: {contracts_aggregates["total_payments"]:,}
Passengers (Taxi/Ambulance): {passengers_aggregates["total_payments"]:,}
Tow Requests: {tow_requests_aggregates["total_payments"]:,}

## Top GDP Contributors
{top_players_str}
    """)

    @app_commands.command(
        name="government_funding", description="Send government funding to player"
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(character_id="Character name")
    @app_commands.autocomplete(discord_user_id=player_autocomplete, character_id=character_autocomplete)
    async def government_funding(
        self,
        interaction,
        discord_user_id: str,
        character_id: int,
        amount: int,
        reason: str,
    ):
        await interaction.response.defer()
        try:
            character = await Character.objects.aget(id=character_id)
            await send_fund_to_player(amount, character, reason)
            await interaction.followup.send(
                f"Government funding deposited into {character.name}'s bank account.\nAmount: {amount:,}\nReason: {reason}"
            )
        except Exception as e:
            await interaction.followup.send(f"Failed to send government funding: {e}")

    @app_commands.command(name="donors", description="List the top donors")
    async def donors(self, interaction):
        await interaction.response.defer()
        contributors = (
            LedgerEntry.objects.filter_donations()
            .select_related("journal_entry", "journal_entry__creator")
            .values("journal_entry__creator")
            .annotate(
                total_contribution=Sum("credit"), name=F("journal_entry__creator__name")
            )
            .order_by("-total_contribution")
        )[:100]

        # Build the contributors string, with a fallback message if empty
        contributors_list = [
            f"**{contribution['name']}:** {contribution['total_contribution']:,}"
            async for contribution in contributors
        ]
        contributors_str = (
            "\n".join(contributors_list)
            if contributors_list
            else "No donations recorded yet."
        )
        # Create the embed
        embed = discord.Embed(
            title="❤️ Top Donors",
            description=contributors_str,
            color=discord.Color.gold(),
            timestamp=timezone.now(),
        )
        embed.set_footer(text=f"Requested by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="top_haulers", description="List the top haulers for a given period"
    )
    @app_commands.describe(
        num_days="The number of days before today to start searching from",
        top_n="The number of top players to show",
    )
    async def top_haulers_cmd(self, interaction, num_days: int = 1, top_n: int = 5):
        await interaction.response.defer()
        embed = await self.build_top_haulers_embed(days=num_days, top_n=top_n)
        embed.set_footer(text=f"Requested by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

    async def build_top_haulers_embed(self, days=1, top_n=5):
        haulers = (
            Delivery.objects.filter(
                timestamp__gte=timezone.now() - timedelta(days=days)
            )
            .values("character")
            .annotate(
                total_payment=Sum(F("payment") + F("subsidy")),
                num_deliveries=Count("id"),
                character_name=F("character__name"),
            )
            .order_by("-total_payment")
        )[:top_n]

        haulers_list = [
            f"**{hauler['character_name']}:** {hauler['total_payment']:,} ({hauler['num_deliveries']} deliveries)"
            async for hauler in haulers
        ]
        haulers_str = (
            "\n".join(haulers_list) if haulers_list else "No deliveries recorded yet."
        )
        # Create the embed
        embed = discord.Embed(
            title="🚚️ Top Haulers",
            description=f"The top {top_n} players for the last {days} day(s)",
            color=discord.Color.gold(),
            timestamp=timezone.now(),
        )
        embed.add_field(
            name="By payment (incl. subsidies)",
            value=haulers_str,
            inline=False,
        )
        return embed

    @app_commands.command(
        name="treasury_stats", description="Display treasury and donations info"
    )
    async def treasury_stats(self, interaction):
        await interaction.response.defer()
        today = timezone.now().date()
        treasury_fund, _ = await Account.objects.aget_or_create(
            account_type=Account.AccountType.ASSET,
            book=Account.Book.GOVERNMENT,
            character=None,
            name="Treasury Fund",
        )
        treasury_fund_in_bank, _ = await Account.objects.aget_or_create(
            account_type=Account.AccountType.ASSET,
            book=Account.Book.GOVERNMENT,
            character=None,
            name="Treasury Fund (in Bank)",
        )
        bank_assets_aggregate = await Account.objects.filter(
            account_type=Account.AccountType.ASSET,
            book=Account.Book.BANK,
        ).aaggregate(
            total_assets=Sum("balance", default=0),
            total_loans=Sum("balance", default=0, filter=Q(character__isnull=False)),
            total_vault=Sum("balance", default=0, filter=Q(character__isnull=True)),
        )

        subsidies_agg = await (
            LedgerEntry.objects.filter_subsidies()
            .filter(journal_entry__date=today)
            .aaggregate(total_subsidies=Sum("debit", default=0))
        )
        contributors = (
            LedgerEntry.objects.filter_donations()
            .select_related("journal_entry", "journal_entry__creator")
            .values("journal_entry__creator")
            .annotate(
                total_contribution=Sum("credit"), name=F("journal_entry__creator__name")
            )
            .order_by("-total_contribution")
        )[:20]

        # Build the contributors string, with a fallback message if empty
        contributors_list = [
            f"**{contribution['name']}:** {contribution['total_contribution']:,}"
            async for contribution in contributors
        ]
        contributors_str = (
            "\n".join(contributors_list)
            if contributors_list
            else "No donations recorded yet."
        )

        # Create the embed
        embed = discord.Embed(
            title="📈 Treasury Report",
            description=f"Status as of {today.strftime('%A, %-d %B %Y')}",
            color=discord.Color.gold(),
            timestamp=timezone.now(),
        )

        # Add Treasury field
        treasury_value = (
            f"**Vault Balance:** `{treasury_fund.balance:,}`\n"
            f"**Bank Deposit:** `{treasury_fund_in_bank.balance:,}`"
        )
        embed.add_field(
            name="💰 Government Treasury", value=treasury_value, inline=False
        )

        # Add Bank of ASEAN field
        bank_value = (
            f"**Total Assets:** `{bank_assets_aggregate['total_assets']:,}`\n"
            f"**Outstanding Loans:** `{bank_assets_aggregate['total_loans']:,}`\n"
            f"**Vault Cash:** `{bank_assets_aggregate['total_vault']:,}`"
        )
        embed.add_field(name="🏦 Bank of ASEAN", value=bank_value, inline=False)

        # Add Subsidies field
        subsidies_value = (
            f"**Today's Disbursements:** `{subsidies_agg['total_subsidies']:,}`"
        )
        embed.add_field(name="💸 Subsidies", value=subsidies_value, inline=False)

        # Add Top Donors field
        embed.add_field(name="❤️ Top Donors", value=contributors_str, inline=False)

        embed.set_footer(text=f"Requested by {interaction.user.display_name}")

        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="treasury_summary",
        description="Display daily treasury income/expense summary with breakdown",
    )
    @app_commands.describe(days="Number of days to summarize (default: 1)")
    async def treasury_summary_cmd(self, interaction, days: int = 1):
        await interaction.response.defer()
        embed = await self.build_treasury_summary_embed(days=days)
        embed.set_footer(text=f"Requested by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

    async def build_treasury_summary_embed(self, days=1):
        summary = await sync_to_async(get_treasury_summary)(days=days)
        now = timezone.now()
        yesterday = (now - timedelta(days=1)).date()

        if days == 1:
            title = "📊 Daily Treasury Summary"
            description = f"Generated for {yesterday.strftime('%A, %-d %B %Y')}"
        else:
            title = f"📊 Treasury Summary ({days} days)"
            description = f"{summary['date_start']} to {summary['date_end']}"

        surplus = summary["surplus"]
        if surplus >= 0:
            surplus_indicator = f"✅ Surplus: `${surplus:,}`"
            color = discord.Color.green()
        else:
            surplus_indicator = f"❌ Deficit: `${surplus:,}`"
            color = discord.Color.red()

        embed = discord.Embed(
            title=title,
            description=f"{description}\n\n{surplus_indicator}",
            color=color,
            timestamp=now,
        )

        # Balances
        embed.add_field(
            name="💰 Treasury Balance",
            value=f"`${summary['treasury_balance']:,}`",
            inline=True,
        )
        embed.add_field(
            name="🏛️ Sovereign Reserves",
            value=f"`${summary['reserves_balance']:,}`",
            inline=True,
        )

        # Income breakdown
        income_lines = []
        for key, data in summary["income"]["breakdown"].items():
            income_lines.append(
                f"{data['emoji']} **{data['label']}:** `${data['amount']:,}`"
            )
        income_str = "\n".join(income_lines) if income_lines else "No income recorded."
        embed.add_field(
            name=f"📈 Total Income: `${summary['income']['total']:,}`",
            value=income_str,
            inline=False,
        )

        # Expense breakdown
        expense_lines = []
        for key, data in summary["expenses"]["breakdown"].items():
            expense_lines.append(
                f"{data['emoji']} **{data['label']}:** `${data['amount']:,}`"
            )
        expense_str = (
            "\n".join(expense_lines) if expense_lines else "No expenses recorded."
        )
        embed.add_field(
            name=f"📉 Total Expenses: `${summary['expenses']['total']:,}`",
            value=expense_str,
            inline=False,
        )

        # Wealth tax (separate — goes to reserves)
        if summary["wealth_tax_collected"] > 0:
            embed.add_field(
                name="🔒 Wealth Tax → Reserves",
                value=f"`${summary['wealth_tax_collected']:,}`",
                inline=True,
            )

        embed.add_field(
            name="🔗 Full Dashboard",
            value="[View on gov.aseanmotorclub.com](https://gov.aseanmotorclub.com/treasury-dashboard)",
            inline=False,
        )

        return embed

    @app_commands.command(name="bank_account", description="Display your bank account")
    async def bank_account(self, interaction):
        try:
            player = await Player.objects.aget(discord_user_id=interaction.user.id)
        except Player.DoesNotExist:
            await interaction.response.send_message(
                "You first need to be verified. Use /verify", ephemeral=True
            )
            return
        character = (
            await player.characters.with_last_login()
            .filter(last_login__isnull=False)
            .alatest("last_login")
        )
        balance = await get_player_bank_balance(character)
        loan_balance = await get_player_loan_balance(character)
        max_loan, max_loan_reason = await get_character_max_loan(character)
        saving_rate = (
            character.saving_rate
            if character.saving_rate is not None
            else Decimal(DEFAULT_SAVING_RATE)
        )
        await interaction.response.send_message(
            f"""\
# Your Bank ASEAN Account

**Owner:** {character.name}
**Balance:** `{balance:,}`
-# Daily Interest Rate: `2.2%` (offline), `4.4%` (online). Interest reduces for balances above $10M.
**Loans:** `{loan_balance:,}`
<Bold>Max Available Loan:</> <Money>{max_loan:,}</>
<Small>{max_loan_reason or "Max available loan depends on your driver+trucking level"}</>
**Earnings Saving Rate:** `{saving_rate * Decimal(100):.0f}%`

### How to Put Money in the Bank
You can only fill your bank account by saving your earnings on this server.
Use `/set_saving_rate` in the game to set how much you want to save. It's 0 by default.
Once you withdraw your balance, you will not be able to deposit them back in.

### How ASEAN Loan Works
Our loans have a flat one-off 10% fee, and you only have to repay them when you make a profit.
The repayment will range from 10% to 40% of your income, depending on the amount of loan you took.
""",
            ephemeral=True,
        )

    @app_commands.command(
        name="treasury_liquidity_injection",
        description="Injects liquidity into the bank from treasury",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def treasury_bank_deposit(self, interaction, amount: int, description: str):
        now = timezone.now()
        await make_treasury_bank_deposit(amount, description)
        await interaction.response.send_message(f"""\
# GOVERNMENT TREASURY: OFFICIAL TRANSACTION RECORD

**Date & Time:** {now.strftime("%d %B %Y, %I:%M %p")}
**Action Type:** {description}

### Transaction Details
- Originating Entity: Office of the Treasury
- Receiving Entity: aseanbank
- Transaction Method: Treasury Direct Deposit
- Amount: {amount:,}

### Purpose & Authorization
This transaction was authorized under Treasury Mandate 2.1 (Financial Mechanism and Liquidity Maintanance).
The purpose of this transfer is to ensure sufficient liquidity within the server's regulated financial system, promoting stability and confidence.
""")

    @app_commands.command(
        name="treasury_bank_withdrawal",
        description="Withdraws funds from the bank back to the treasury",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def treasury_bank_withdrawal(
        self, interaction, amount: int, description: str
    ):
        now = timezone.now()
        try:
            await make_treasury_bank_withdrawal(amount, description)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"""\
# GOVERNMENT TREASURY: OFFICIAL TRANSACTION RECORD

**Date & Time:** {now.strftime("%d %B %Y, %I:%M %p")}
**Action Type:** {description}

### Transaction Details
- Originating Entity: aseanbank
- Receiving Entity: Office of the Treasury
- Transaction Method: Treasury Direct Withdrawal
- Amount: {amount:,}

### Purpose & Authorization
This transaction was authorized under Treasury Mandate 2.1 (Financial Mechanism and Liquidity Maintenance).
The purpose of this transfer is to return funds from the bank to the government treasury.
""")

    @app_commands.command(
        name="taxpayers",
        description="List top/bottom players by donation-to-earnings ratio.",
    )
    async def taxpayers(self, interaction, num_days: int = 30):
        await interaction.response.defer()
        start_time, end_time = get_timespan(num_days, num_days)

        # Subqueries for each earning type
        delivery_sum_subquery = (
            ServerCargoArrivedLog.objects.filter(
                player=OuterRef("pk"), timestamp__gte=start_time, timestamp__lt=end_time
            )
            .values("player")
            .annotate(total=Sum("payment"))
            .values("total")
        )

        contracts_sum_subquery = (
            ServerSignContractLog.objects.filter(
                player=OuterRef("pk"), timestamp__gte=start_time, timestamp__lt=end_time
            )
            .values("player")
            .annotate(total=Sum("payment"))
            .values("total")
        )

        passengers_sum_subquery = (
            ServerPassengerArrivedLog.objects.filter(
                player=OuterRef("pk"), timestamp__gte=start_time, timestamp__lt=end_time
            )
            .values("player")
            .annotate(total=Sum("payment"))
            .values("total")
        )

        tow_requests_sum_subquery = (
            ServerTowRequestArrivedLog.objects.filter(
                player=OuterRef("pk"), timestamp__gte=start_time, timestamp__lt=end_time
            )
            .values("player")
            .annotate(total=Sum("payment"))
            .values("total")
        )

        # Subquery for donations
        donations_sum_subquery = (
            LedgerEntry.objects.filter(
                journal_entry__creator__player=OuterRef("pk"),
                journal_entry__created_at__gte=start_time,
                journal_entry__created_at__lte=end_time,
            )
            .filter_donations()
            .values("journal_entry__creator__player")
            .annotate(total=Sum("credit", default=0))
            .values("total")
        )

        progressive_case = get_progressive_donation_case(DONATION_EXPECTATION_BRACKETS)

        # Main Query to get player stats
        player_stats_qs = (
            Player.objects.annotate(
                total_earnings=Coalesce(
                    Subquery(delivery_sum_subquery, output_field=DecimalField()),
                    Value(Decimal(0)),
                )
                + Coalesce(
                    Subquery(contracts_sum_subquery, output_field=DecimalField()),
                    Value(Decimal(0)),
                )
                + Coalesce(
                    Subquery(passengers_sum_subquery, output_field=DecimalField()),
                    Value(Decimal(0)),
                )
                + Coalesce(
                    Subquery(tow_requests_sum_subquery, output_field=DecimalField()),
                    Value(Decimal(0)),
                )
            )
            .annotate(
                total_donations=Coalesce(
                    Subquery(donations_sum_subquery, output_field=DecimalField()),
                    Value(Decimal(0)),
                )
            )
            .filter(total_earnings__gt=0)
            .annotate(
                expected_donation=progressive_case,
                contribution_delta=F("total_donations") - F("expected_donation"),
            )
        )

        top_10_qs = player_stats_qs.order_by("-contribution_delta")[:10]
        bottom_10_qs = player_stats_qs.order_by("contribution_delta")[:10]

        async def get_player_name(player):
            if player.discord_user_id:
                try:
                    user = await interaction.guild.fetch_member(player.discord_user_id)
                    return user.display_name
                except discord.NotFound:
                    pass
            try:
                latest_character = await (
                    Character.objects.with_last_login()
                    .filter(player=player, last_login__isnull=False)
                    .alatest("last_login")
                )
                return latest_character.name
            except Character.DoesNotExist:
                return player.unique_id
            except Exception:
                return f"Character not found ({player.unique_id})"

        async def format_player_list(qs):
            lines = []
            async for p in qs:
                name = await get_player_name(p)
                line = (
                    f"**{name}**: {p.contribution_delta:+,} "
                    f"(Donated: `{p.total_donations:,.0f}`, Expected: `{p.expected_donation:,.0f}`)"
                )
                lines.append(line)
            return "\n".join(lines) if lines else "Not enough data."

        top_10_str = await format_player_list(top_10_qs)
        bottom_10_str = await format_player_list(bottom_10_qs)

        embed = discord.Embed(
            title="📊 Taxpayer Leaderboard",
            description=f"Ranking based on donation amount vs. expectation over the last **{num_days}** days.",
            color=discord.Color.blue(),
            timestamp=timezone.now(),
        )
        embed.add_field(
            name="✅ Top 10 Contributors (Above Expectation)",
            value=top_10_str,
            inline=False,
        )
        embed.add_field(
            name="✴️ Bottom 10 Contributors (Below Expectation)",
            value=bottom_10_str,
            inline=False,
        )
        embed.set_footer(text=f"Requested by {interaction.user.display_name}")

        await interaction.followup.send(embed=embed)

    # --- NPL Management Features ---

    @tasks.loop(time=dt_time(hour=3, minute=0, tzinfo=dt_timezone.utc))
    async def npl_warning_task(self):
        """Send a one-time DM warning to players with non-performing loans."""
        logger = logging.getLogger("amc.npl")
        npl_accounts = await sync_to_async(get_non_performing_loans)()
        warned = 0
        for account in npl_accounts:
            if account.npl_warning_sent_at is not None:
                # Re-send warning if it's been a full period since the last warning
                if timezone.now() < account.npl_warning_sent_at + timedelta(
                    days=account.repayment_period_days
                ):
                    continue
            player = account.character.player
            if not player or not player.discord_user_id:
                continue
            try:
                user = await self.bot.fetch_user(player.discord_user_id)
                await user.send(
                    f"📋 **Courtesy Notice from the Bank of ASEAN**\n\n"
                    f"Your loan for character **{account.character.name}** is behind "
                    f"on its payment plan.\n"
                    f"Outstanding balance: **${account.balance:,.0f}**\n"
                    f"Repaid this period: **${account.total_repaid_in_period:,.0f}** "
                    f"/ **${account.min_required_repayment:,.0f}** required\n\n"
                    f"Please make deliveries or repayments to meet the minimum. "
                    f"Accounts that remain behind may be publicly listed on the Collections Board."
                )
                warned += 1
            except discord.Forbidden:
                logger.info(f"Cannot DM user {player.discord_user_id} (DMs disabled)")
            except Exception:
                logger.exception(
                    f"Failed to send NPL warning to {player.discord_user_id}"
                )
                continue
            account.npl_warning_sent_at = timezone.now()
            await account.asave(update_fields=["npl_warning_sent_at"])

        if warned:
            logger.info(f"Sent {warned} NPL warning DMs")

    @tasks.loop(time=dt_time(hour=4, minute=0, tzinfo=dt_timezone.utc))
    async def crossover_warning_task(self):
        """Daily DM to players whose wealth tax exceeds their interest."""
        logger = logging.getLogger("amc.crossover")
        crossover_accounts = await sync_to_async(get_crossover_accounts)()
        warned = 0
        for account in crossover_accounts:
            character = account.character
            if character.crossover_warning_sent_at is not None:
                if timezone.now() < character.crossover_warning_sent_at + timedelta(
                    days=30
                ):
                    continue
            player = character.player
            if not player or not player.discord_user_id:
                continue
            try:
                user = await self.bot.fetch_user(player.discord_user_id)
                await user.send(
                    f"📊 **Financial Advisory from the Bank of ASEAN**\n\n"
                    f"Your bank account for **{character.name}** has reached a point where your "
                    f"hourly **wealth tax exceeds your interest earnings**.\n\n"
                    f"**Current Balance:** ${account.balance:,.0f}\n"
                    f"**Hourly Interest:** +${account.hourly_interest:,}\n"
                    f"**Hourly Wealth Tax:** -${account.hourly_tax:,}\n"
                    f"**Net Hourly Change:** -${account.net_hourly_loss:,}\n\n"
                    f"Your balance is now decreasing every hour you remain offline.\n"
                    f"Log back in — even briefly — to reset your tax clock and resume earning full interest."
                )
                warned += 1
            except discord.Forbidden:
                logger.info(f"Cannot DM user {player.discord_user_id} (DMs disabled)")
            except Exception:
                logger.exception(
                    f"Failed to send crossover warning to {player.discord_user_id}"
                )
                continue
            character.crossover_warning_sent_at = timezone.now()
            await character.asave(update_fields=["crossover_warning_sent_at"])

        if warned:
            logger.info(f"Sent {warned} crossover warning DMs")

    @tasks.loop(time=dt_time(hour=8, minute=30, tzinfo=dt_timezone.utc))
    async def npl_collections_board_task(self):
        """Post a weekly public Collections Board for NPL accounts."""
        if timezone.now().weekday() != 6:  # Only on Sundays
            return

        npl_accounts = await sync_to_async(get_non_performing_loans)()
        # Sort by balance descending
        npl_accounts.sort(key=lambda a: a.balance, reverse=True)

        lines = []
        total_outstanding = 0
        for account in npl_accounts:
            pct = 0
            if account.min_required_repayment > 0:
                pct = int(
                    account.total_repaid_in_period
                    / account.min_required_repayment
                    * 100
                )
            lines.append(
                f"**{account.character.name}** — `${account.balance:,.0f}` (repaid {pct}% of minimum)"
            )
            total_outstanding += account.balance

        if not lines:
            return

        description = "\n".join(lines[:25])
        if len(lines) > 25:
            description += f"\n\n*...and {len(lines) - 25} more accounts*"

        embed = discord.Embed(
            title="🏦 Bank of ASEAN — Collections Board",
            description=(
                "The following accounts have not met the minimum repayment "
                "for their payment plan period.\n\n" + description
            ),
            color=discord.Color.red(),
            timestamp=timezone.now(),
        )
        embed.add_field(
            name="Total Outstanding",
            value=f"`${total_outstanding:,.0f}`",
            inline=True,
        )
        embed.add_field(
            name="Accounts Listed",
            value=f"`{len(lines)}`",
            inline=True,
        )
        embed.set_footer(
            text="Make deliveries or repayments to clear your name from this list."
        )

        treasury_channel_id = getattr(
            settings, "DISCORD_TREASURY_CHANNEL_ID", 1402660537619320872
        )
        treasury_channel = self.bot.get_channel(treasury_channel_id)
        if treasury_channel:
            sent_message = await treasury_channel.send(embed=embed)
            general_channel = self.bot.get_channel(self.general_channel_id)
            if general_channel:
                await sent_message.forward(general_channel)

    @app_commands.command(
        name="npl",
        description="List non-performing loan accounts by repayment shortfall",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def npl_command(self, interaction):
        await interaction.response.defer(ephemeral=True)

        npl_accounts = await sync_to_async(get_non_performing_loans)()

        if not npl_accounts:
            await interaction.followup.send(
                "No non-performing loans found.", ephemeral=True
            )
            return

        # Sort by shortfall (required - repaid) descending
        npl_accounts.sort(
            key=lambda a: a.min_required_repayment - a.total_repaid_in_period,
            reverse=True,
        )

        # Build compact table
        header = f"{'Name':<16} | {'Balance':>10} | {'Repaid':>8} | {'Req':>8} | {'Per':>3}\n"
        separator = f"{'-' * 16}-+-{'-' * 10}-+-{'-' * 8}-+-{'-' * 8}-+-{'-' * 3}\n"
        table_lines = []
        total_balance = 0

        for account in npl_accounts:
            name = account.character.name[:16]
            repaid = account.total_repaid_in_period
            required = account.min_required_repayment
            period = account.repayment_period_days
            line = f"{name:<16} | ${account.balance:>9,.0f} | ${repaid:>7,.0f} | ${required:>7,.0f} | {period:>2}d"
            table_lines.append(line)
            total_balance += account.balance

        # Truncate to fit embed
        shown_lines = []
        total_len = len(header) + len(separator)
        for line in table_lines:
            if total_len + len(line) + 60 > 4096:
                break
            shown_lines.append(line)
            total_len += len(line) + 1

        embed = discord.Embed(
            title="📊 Non-Performing Loans Report",
            description=f"```\n{header}{separator}" + "\n".join(shown_lines) + "\n```",
            color=discord.Color.orange(),
            timestamp=timezone.now(),
        )
        if len(shown_lines) < len(table_lines):
            embed.add_field(
                name="Truncated",
                value=f"Showing {len(shown_lines)} of {len(table_lines)} accounts",
                inline=True,
            )
        embed.add_field(
            name="Total Outstanding",
            value=f"`${total_balance:,.0f}`",
            inline=True,
        )
        from amc_finance.loans import (
            NPL_DEFAULT_REPAYMENT_RATE,
            NPL_DEFAULT_PERIOD_DAYS,
        )

        embed.set_footer(
            text=f"Default plan: {int(NPL_DEFAULT_REPAYMENT_RATE * 100)}% per {NPL_DEFAULT_PERIOD_DAYS}d | Req = required, Per = period"
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="set_interest_rate",
        description="Set the bank's daily interest rate (Admin / Finance Minister)",
    )
    @app_commands.checks.has_any_role(
        settings.DISCORD_ADMIN_ROLE_ID, settings.DISCORD_FINANCE_MINISTER_ROLE_ID
    )
    @app_commands.describe(
        rate_percent="Nominal daily rate in percent (e.g. 2.2 = 2.2%/day, -5 to +5; 0 disables interest)"
    )
    async def set_interest_rate_command(self, interaction, rate_percent: float):
        await interaction.response.defer(ephemeral=True)

        if not (-Decimal(5) <= Decimal(str(rate_percent)) <= Decimal(5)):
            await interaction.followup.send(
                "Rate must be between -5 and +5 percent per day.", ephemeral=True
            )
            return

        new_rate = Decimal(str(rate_percent)) / Decimal(100)
        old_rate = await sync_to_async(BankPolicy.get_daily_interest_rate)()
        await sync_to_async(_set_daily_interest_rate)(new_rate)

        # Announce the change in the treasury channel
        treasury_channel_id = getattr(
            settings, "DISCORD_TREASURY_CHANNEL_ID", 1402660537619320872
        )
        treasury_channel = self.bot.get_channel(treasury_channel_id)
        if treasury_channel:
            embed = discord.Embed(
                title="🏦 Bank Interest Rate Changed",
                description=f"Daily interest rate set to **{new_rate:.2%}** "
                f"by {interaction.user.mention}",
                color=discord.Color.gold(),
            )
            await treasury_channel.send(embed=embed)

        await interaction.followup.send(
            f"Daily interest rate set to **{new_rate:.2%}** (was {old_rate:.2%}).",
            ephemeral=True,
        )

    @app_commands.command(
        name="set_gov_salary",
        description="Set the Government/Police salary UBI multiplier (Admin / Finance Minister)",
    )
    @app_commands.checks.has_any_role(
        settings.DISCORD_ADMIN_ROLE_ID, settings.DISCORD_FINANCE_MINISTER_ROLE_ID
    )
    @app_commands.describe(
        multiplier="Salary = UBI x multiplier (e.g. 2 = gov employees and on-duty police earn 2x UBI; 0-10)"
    )
    async def set_gov_salary_command(self, interaction, multiplier: float):
        await interaction.response.defer(ephemeral=True)

        if not (Decimal(0) <= Decimal(str(multiplier)) <= Decimal(10)):
            await interaction.followup.send(
                "Multiplier must be between 0 and 10.", ephemeral=True
            )
            return

        new_mult = Decimal(str(multiplier)).quantize(Decimal("0.001"))
        old_mult = await sync_to_async(BankPolicy.get_gov_salary_multiplier)()
        await sync_to_async(_set_gov_salary_multiplier)(new_mult)

        treasury_channel_id = getattr(
            settings, "DISCORD_TREASURY_CHANNEL_ID", 1402660537619320872
        )
        treasury_channel = self.bot.get_channel(treasury_channel_id)
        if treasury_channel:
            embed = discord.Embed(
                title="🏛️ Government Salary Rate Changed",
                description=f"Gov/Police salary multiplier set to **{new_mult}x UBI** "
                f"by {interaction.user.mention}",
                color=discord.Color.gold(),
            )
            await treasury_channel.send(embed=embed)

        await interaction.followup.send(
            f"Government/Police salary multiplier set to **{new_mult}x UBI** "
            f"(was {old_mult}x).",
            ephemeral=True,
        )

    @app_commands.command(
        name="set_wealth_tax",
        description="Set the wealth tax multiplier, with simulation preview (Admin / Finance Minister)",
    )
    @app_commands.checks.has_any_role(
        settings.DISCORD_ADMIN_ROLE_ID, settings.DISCORD_FINANCE_MINISTER_ROLE_ID
    )
    @app_commands.describe(
        multiplier="Wealth tax multiplier (e.g. 1 = normal brackets, 0 disables, 2 doubles; 0-10)"
    )
    async def set_wealth_tax_command(self, interaction, multiplier: float):
        await interaction.response.defer(ephemeral=True)

        if not (Decimal(0) <= Decimal(str(multiplier)) <= Decimal(10)):
            await interaction.followup.send(
                "Multiplier must be between 0 and 10.", ephemeral=True
            )
            return

        new_mult = Decimal(str(multiplier)).quantize(Decimal("0.001"))
        old_mult = await sync_to_async(BankPolicy.get_wealth_tax_multiplier)()

        interest_rate = await sync_to_async(BankPolicy.get_daily_interest_rate)()
        pages = await sync_to_async(_wealth_tax_preview_pages)(
            new_mult,
            interest_rate,
            old_mult,
        )
        view = WealthTaxConfirmView(
            cog=self, user_id=interaction.user.id, multiplier=new_mult, old_multiplier=old_mult
        )
        await interaction.followup.send(
            content=(
                f"Review the simulation below, then confirm to set the wealth tax "
                f"multiplier to **{new_mult}x** (currently {old_mult}x).\n"
                + pages[0]
            ),
            ephemeral=True,
        )
        await interaction.followup.send(
            content=pages[1], view=view, ephemeral=True
        )

    @commands.Cog.listener()
    async def on_message(self, message):
        if (
            message.channel.id == self.decrypt_save_file_channel_id
            and not message.author.bot
        ):
            channel = message.channel
            attachments = message.attachments
            if not attachments:
                await channel.send_message("You need to attach a file")
            if len(attachments) > 1:
                await channel.send_message("1 save file at a time please")

            attachment = attachments[0]
            attachment_bytes = await attachment.read()
            if re.match(".*\.json", attachment.filename):
                file_bytes = encrypt(attachment_bytes)
                file_ext = "sav"
            else:
                file_bytes = decrypt(attachment_bytes)
                file_ext = "json"

            await channel.send(
                file=discord.File(
                    fp=BytesIO(file_bytes), filename=f"{attachment.filename}.{file_ext}"
                ),
                reference=message,
            )
