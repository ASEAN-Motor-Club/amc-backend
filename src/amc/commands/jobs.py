from amc.command_framework import registry, CommandContext
from amc.models import DeliveryJob, JobPostingConfig, BotInvocationLog
from amc.utils import get_time_difference_string
from amc.subsidies import get_subsidies_text
from amc.jobs import calculate_treasury_multiplier
from amc_finance.services import get_treasury_fund_balance
from amc.webhook import PARTY_BONUS_ENABLED, PARTY_BONUS_RATE
from amc.mod_server import get_parties, get_party_size_for_character
from django.db.models import F
from django.utils.translation import gettext as _, gettext_lazy


@registry.register(
    "/jobs",
    description=gettext_lazy("List available server jobs"),
    category="Jobs",
    featured=True,
)
async def cmd_jobs(ctx: CommandContext):
    jobs = DeliveryJob.objects.filter(
        quantity_fulfilled__lt=F("quantity_requested"),
        expired_at__gte=ctx.timestamp,
    ).prefetch_related("source_points", "destination_points", "cargos")

    # Calculate treasury boost
    config = await JobPostingConfig.aget_config()
    treasury_balance = await get_treasury_fund_balance()
    treasury_mult = calculate_treasury_multiplier(
        float(treasury_balance),
        equilibrium=float(config.treasury_equilibrium),
        sensitivity=float(config.treasury_sensitivity),
        cap_ratio=float(config.treasury_cap_ratio),
    )
    boost_pct = int(treasury_mult * 100)
    if treasury_mult >= 1.0:
        boost_str = f"<EffectGood>{boost_pct}%</>"
    else:
        boost_str = f"<EffectBad>{boost_pct}%</>"

    # Party bonus info (only when feature is enabled)
    party_str = ""
    if PARTY_BONUS_ENABLED and ctx.http_client_mod:
        parties = await get_parties(ctx.http_client_mod)
        party_size = get_party_size_for_character(parties, str(ctx.character.guid))
        bonus_pct = int((party_size - 1) * PARTY_BONUS_RATE * 100)
        if party_size > 1:
            party_str = _(
                "\n<Secondary>Party Bonus:</> <EffectGood>+{bonus_pct}%</> ({party_size} members)"
                "\n<Secondary>Form larger parties for bigger bonuses!</>"
            ).format(bonus_pct=bonus_pct, party_size=party_size)
        else:
            rate_pct = int(PARTY_BONUS_RATE * 100)
            party_str = _(
                "\n<Secondary>Party Bonus:</> <EffectBad>None (solo)</>"
                "\n<Secondary>Join a party for +{rate_pct}% per member!</>"
            ).format(rate_pct=rate_pct)

    jobs_str_list: list[str] = []
    async for job in jobs:
        cargo_key = (
            job.get_cargo_key_display()
            if job.cargo_key
            else ", ".join([c.label for c in job.cargos.all()])
        )
        title = f"({job.quantity_fulfilled}/{job.quantity_requested}) {job.name} · <Money>{job.completion_bonus:,}</>"
        title += "\n" + _("<Secondary>Expiring in {time}</>").format(
            time=get_time_difference_string(ctx.timestamp, job.expired_at)
        )
        title += "\n" + _("<Secondary>Cargo: {cargo_key}</>").format(
            cargo_key=cargo_key
        )

        if source_points := list(job.source_points.all()):
            title += "\n" + _("<Secondary>ONLY from: {points}</>").format(
                points=", ".join([p.name for p in source_points])
            )

        if destination_points := list(job.destination_points.all()):
            title += "\n" + _("<Secondary>ONLY to: {points}</>").format(
                points=", ".join([p.name for p in destination_points])
            )

        jobs_str_list.append(title)

    jobs_str = "\n\n".join(jobs_str_list)
    await ctx.reply(
        _(
            "<Title>Delivery Jobs</>"
            "\n<Secondary>Complete jobs solo or with others!</>"
            "\n<Secondary>Treasury Boost:</> {boost_str}"
            "{party_str}"
            "\n\n{jobs_str}"
            "\n\n<Title>Subsidies</>: Use /subsidies to view."
        ).format(
            jobs_str=jobs_str,
            boost_str=boost_str,
            party_str=party_str,
        )
    )


@registry.register(
    "/subsidies",
    description=gettext_lazy("View job subsidies information"),
    category="Jobs",
)
async def cmd_subsidies(ctx: CommandContext):
    await ctx.reply(await get_subsidies_text())
    await BotInvocationLog.objects.acreate(
        timestamp=ctx.timestamp, character=ctx.character, prompt="subsidies"
    )
