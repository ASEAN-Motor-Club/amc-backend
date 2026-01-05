from amc.command_framework import registry, CommandContext
from amc.models import DeliveryJob, BotInvocationLog
from amc.mod_server import get_rp_mode
from amc.utils import get_time_difference_string
from amc.subsidies import get_subsidies_text
from django.db.models import F
from django.utils.translation import gettext as _, gettext_lazy

@registry.register("/jobs", description=gettext_lazy("List available server jobs"), category="Jobs", featured=True)
async def cmd_jobs(ctx: CommandContext):
    is_rp_mode = await get_rp_mode(ctx.http_client_mod, ctx.character.guid)
    jobs = DeliveryJob.objects.filter(
        quantity_fulfilled__lt=F('quantity_requested'),
        expired_at__gte=ctx.timestamp,
    ).prefetch_related('source_points', 'destination_points', 'cargos')

    jobs_str_list: list[str] = []
    async for job in jobs:
        cargo_key = job.get_cargo_key_display() if job.cargo_key else ', '.join([c.label for c in job.cargos.all()])
        title = f"({job.quantity_fulfilled}/{job.quantity_requested}) {job.name} · <EffectGood>{job.bonus_multiplier*100:.0f}%</> · <Money>{job.completion_bonus:,}</>"
        if job.rp_mode:
            title += "\n" + _("<Warning>Requires RP Mode</> (Yours: {status})").format(status='<EffectGood>ON</>' if is_rp_mode else '<Warning>OFF</>')
        title += "\n" + _("<Secondary>Expiring in {time}</>").format(time=get_time_difference_string(ctx.timestamp, job.expired_at))
        title += "\n" + _("<Secondary>Cargo: {cargo_key}</>").format(cargo_key=cargo_key)
        
        if source_points := list(job.source_points.all()):
            title += "\n" + _("<Secondary>ONLY from: {points}</>").format(points=', '.join([p.name for p in source_points]))
        
        if destination_points := list(job.destination_points.all()):
             title += "\n" + _("<Secondary>ONLY to: {points}</>").format(points=', '.join([p.name for p in destination_points]))
             
        jobs_str_list.append(title)

    jobs_str = "\n\n".join(jobs_str_list)
    await ctx.reply(_("""<Title>Delivery Jobs</>
<Secondary>Complete jobs solo or with others!</>

{jobs_str}

<Title>RP Mode</>: {rp_status} (/rp_mode)
<Title>Subsidies</>: Use /subsidies to view.""").format(
        jobs_str=jobs_str,
        rp_status='<EffectGood>ON</>' if is_rp_mode else '<Warning>OFF</>'
    ))

@registry.register("/subsidies", description=gettext_lazy("View job subsidies information"), category="Jobs")
async def cmd_subsidies(ctx: CommandContext):
    await ctx.reply(await get_subsidies_text())
    await BotInvocationLog.objects.acreate(timestamp=ctx.timestamp, character=ctx.character, prompt="subsidies")
