from amc.command_framework import registry, CommandContext
from amc.models import DeliveryJob, BotInvocationLog
from amc.mod_server import get_rp_mode
from amc.utils import get_time_difference_string
from amc.subsidies import SUBSIDIES_TEXT
from django.db.models import F

@registry.register("/jobs", description="List available server jobs", category="Jobs")
async def cmd_jobs(ctx: CommandContext):
    is_rp_mode = await get_rp_mode(ctx.http_client_mod, ctx.character.guid)
    jobs = DeliveryJob.objects.filter(
        quantity_fulfilled__lt=F('quantity_requested'),
        expired_at__gte=ctx.timestamp,
    ).prefetch_related('source_points', 'destination_points', 'cargos')

    jobs_str_list = []
    async for job in jobs:
        cargo_key = job.get_cargo_key_display() if job.cargo_key else ', '.join([c.label for c in job.cargos.all()])
        title = f"({job.quantity_fulfilled}/{job.quantity_requested}) {job.name} · <EffectGood>{job.bonus_multiplier*100:.0f}%</> · <Money>{job.completion_bonus:,}</>"
        if job.rp_mode:
            title += f"\n<Warning>Requires RP Mode</> (Yours: {'<EffectGood>ON</>' if is_rp_mode else '<Warning>OFF</>'})"
        title += f"\n<Secondary>Expiring in {get_time_difference_string(ctx.timestamp, job.expired_at)}</>"
        title += f"\n<Secondary>Cargo: {cargo_key}</>"
        jobs_str_list.append(title)

    jobs_str = "\n\n".join(jobs_str_list)
    await ctx.reply(f"""<Title>Delivery Jobs</>
<Secondary>Complete jobs solo or with others!</>

{jobs_str}

<Title>RP Mode</>: {'<EffectGood>ON</>' if is_rp_mode else '<Warning>OFF</>'} (/rp_mode)
<Title>Subsidies</>: Use /subsidies to view.""")

@registry.register("/subsidies", description="View job subsidies information", category="Jobs")
async def cmd_subsidies(ctx: CommandContext):
    await ctx.reply(SUBSIDIES_TEXT)
    await BotInvocationLog.objects.acreate(timestamp=ctx.timestamp, character=ctx.character, prompt="subsidies")
